"""Campione MIMIC-IV Hosp con lettere di riferimento separate dalle fonti."""

import csv
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from codice_tesi.discharge.models import MIMIC_DATASET, Context, EvidenceSource

EXTRACTION = "hosp-note-2.2-expanded-v2"
MAX_CASES = 100
MIXED_EXTRACTION = "hosp-note-ed-icu-2.2-v1"


def rows(root: Path, table: str, *, keys=None, key_columns=("subject_id", "hadm_id")):
    path = root / f"{table}.csv.gz"
    with gzip.open(path, "rt", encoding="utf-8", newline="") as file:
        records = csv.reader(file)
        columns = next(records, [])
        if not columns:
            raise ValueError(f"Intestazione CSV mancante: {table}.")
        if keys is not None:
            left, right = map(columns.index, key_columns)
            records = (record for record in records if (record[left], record[right]) in keys)
        for record in records:
            yield dict(zip(columns, record, strict=True))


def pair(row):
    return row["subject_id"], row["hadm_id"]


def cohort(hosp: Path, notes: Path, *, limit: int, seed: int):
    """Mantiene i primi K hash tra le coppie eleggibili, con tutte le loro note."""
    if not 1 <= limit <= MAX_CASES or seed < 0:
        raise ValueError(f"Usare da 1 a {MAX_CASES} ricoveri e un seed non negativo.")
    eligible = {
        pair(row)
        for row in rows(hosp, "admissions")
        if row["admittime"] and row["admittime"] < row["dischtime"]
    }
    selected = {}
    ranks = {}
    worst = None
    for row in rows(notes, "discharge"):
        key = pair(row)
        if key not in eligible or not row["text"].strip():
            continue
        if key in selected:
            selected[key].append(row)
            continue
        rank = hashlib.sha256(f"{seed}:{key[0]}:{key[1]}".encode()).digest(), key
        if worst is not None and rank >= worst:
            continue
        if worst is not None:
            del selected[worst[1]]
            del ranks[worst[1]]
        ranks[key] = rank
        selected[key] = [row]
        worst = max(ranks.values()) if len(ranks) == limit else None
    if len(selected) != limit:
        raise ValueError("Ricoveri abbinabili insufficienti per il campione richiesto.")
    for references in selected.values():
        references.sort(key=lambda row: (row["storetime"], row["charttime"], row["note_id"]))
        if len({row["note_id"] for row in references}) != len(references):
            raise ValueError("Identificativi duplicati nelle lettere di riferimento.")
    return selected


def index_stays(root, table, admissions):
    stays = {}
    for row in rows(root, table, keys=admissions):
        key = pair(row)
        admission = admissions[key]
        lower = admission["admittime"] if table == "icustays" else "0"
        if not row["stay_id"] or not (
            lower <= row["intime"] <= row["outtime"] <= admission["dischtime"]
        ):
            continue
        identity = row["subject_id"], row["stay_id"]
        if identity in stays:
            raise ValueError(f"Identità di permanenza duplicata in {table}.")
        stays[identity] = key, row
    return stays


def mixed_cohort(hosp, notes, admissions, ed_stays, icu_stays, *, limit, seed):
    """Bilancia i percorsi di cura e privilegia servizi e categorie ICD diversi."""
    ed_keys = {key for key, _ in ed_stays.values()}
    icu_keys = {key for key, _ in icu_stays.values()}
    with_letter = {
        pair(row) for row in rows(notes, "discharge", keys=admissions) if row["text"].strip()
    }
    services = {}
    for row in rows(hosp, "services", keys=with_letter):
        service = row["transfertime"], row["curr_service"]
        key = pair(row)
        if row["curr_service"] and service < services.get(key, ("~", "")):
            services[key] = service
    diagnosis = {
        pair(row): (row["icd_version"], row["icd_code"][:3])
        for row in rows(hosp, "diagnoses_icd", keys=with_letter)
        if row["seq_num"] == "1" and row["icd_code"]
    }
    strata = defaultdict(list)
    for key in with_letter & diagnosis.keys() & services.keys():
        stratum = key in ed_keys, key in icu_keys
        rank = hashlib.sha256(f"{seed}:{key[0]}:{key[1]}".encode()).digest()
        strata[stratum].append((key, services[key][1], diagnosis[key], rank))

    selected, patients = set(), set()
    service_counts, diagnosis_counts = Counter(), Counter()
    for index in range(limit):
        stratum = ((True, True), (True, False), (False, True), (False, False))[index % 4]
        choices = (candidate for candidate in strata[stratum] if candidate[0][0] not in patients)
        best = min(
            choices,
            key=lambda c: (service_counts[c[1]], diagnosis_counts[c[2]], c[3], c[0]),
            default=None,
        )
        if best is None:
            raise ValueError(f"Ricoveri con lettera insufficienti per il percorso {stratum}.")
        key, service, code, _ = best
        selected.add(key)
        patients.add(key[0])
        service_counts[service] += 1
        diagnosis_counts[code] += 1

    references = defaultdict(list)
    for row in rows(notes, "discharge", keys=selected):
        if row["text"].strip():
            references[pair(row)].append(row)
    for letters in references.values():
        letters.sort(key=lambda row: (row["storetime"], row["charttime"], row["note_id"]))
        if len({row["note_id"] for row in letters}) != len(letters):
            raise ValueError("Identificativi duplicati nelle lettere di riferimento.")
    return references


def add_ed(ed, stays, admissions, groups):
    for key, stay in stays.values():
        if key in admissions:
            groups[key]["ed/edstays"].append(
                (
                    stay["intime"],
                    compact(
                        stay, "stay_id", "intime", "outtime", "arrival_transport", "disposition"
                    ),
                )
            )
    for table, label, fields, cap in (
        (
            "triage",
            "Motivo e valutazione al triage",
            (
                "chiefcomplaint",
                "acuity",
                "temperature",
                "heartrate",
                "resprate",
                "o2sat",
                "sbp",
                "dbp",
                "pain",
            ),
            8,
        ),
        (
            "vitalsign",
            "Misure in pronto soccorso",
            (
                "charttime",
                "temperature",
                "heartrate",
                "resprate",
                "o2sat",
                "sbp",
                "dbp",
                "rhythm",
                "pain",
            ),
            16,
        ),
        (
            "diagnosis",
            "Codifica del pronto soccorso",
            (
                "seq_num",
                "icd_code",
                "icd_version",
                "icd_title",
            ),
            20,
        ),
        ("medrecon", "Terapia riferita all'ingresso", ("charttime", "name"), 30),
        ("pyxis", "Prelievo farmaco dal dispensatore", ("charttime", "name"), 20),
    ):
        events = defaultdict(set)
        for row in rows(ed, table, keys=stays, key_columns=("subject_id", "stay_id")):
            key, stay = stays[row["subject_id"], row["stay_id"]]
            time = row.get("charttime", stay["intime"])
            if not stay["intime"] <= time <= stay["outtime"]:
                continue
            detail = compact(row, *fields)
            if detail:
                events[key].add((time, f"{label} | stay_id={row['stay_id']} | {detail}"))
        for key, entries in events.items():
            ordered = sorted(entries)
            groups[key][f"ed/{table}"] = (
                ordered
                if len(ordered) <= cap
                else [*ordered[: cap // 2], *ordered[-(cap - cap // 2) :]]
            )


def add_icu(icu, stays, admissions, groups):
    items = {row["itemid"]: row["label"] for row in rows(icu, "d_items")}
    for key, stay in stays.values():
        groups[key]["icu/icustays"].append(
            (
                stay["intime"],
                compact(stay, "stay_id", "first_careunit", "last_careunit", "intime", "outtime"),
            )
        )
    for table, label, start, stop, fields, cap in (
        (
            "chartevents",
            "Misura ICU",
            "charttime",
            "charttime",
            ("value", "valuenum", "valueuom"),
            36,
        ),
        ("datetimeevents", "Data registrata ICU", "charttime", "charttime", ("value",), 10),
        (
            "outputevents",
            "Uscita registrata ICU",
            "charttime",
            "charttime",
            ("value", "valueuom"),
            12,
        ),
        (
            "inputevents",
            "Evento di infusione ICU",
            "starttime",
            "endtime",
            ("endtime", "amount", "amountuom", "rate", "rateuom", "orderid", "statusdescription"),
            24,
        ),
        (
            "ingredientevents",
            "Componente dell'infusione ICU",
            "starttime",
            "endtime",
            ("endtime", "amount", "amountuom", "rate", "rateuom", "orderid", "statusdescription"),
            12,
        ),
        (
            "procedureevents",
            "Evento procedurale ICU",
            "starttime",
            "endtime",
            ("endtime", "value", "valueuom", "orderid", "statusdescription"),
            20,
        ),
    ):
        series = defaultdict(dict)
        for row in rows(icu, table, keys=stays, key_columns=("subject_id", "stay_id")):
            key, stay = stays[row["subject_id"], row["stay_id"]]
            time = row[start]
            if pair(row) != key or not (
                stay["intime"] <= time <= row[stop] <= stay["outtime"]
                and time <= row["storetime"] <= admissions[key]["dischtime"]
            ):
                continue
            detail = compact(row, *fields)
            if not detail or len(detail) > 300 or "\n" in detail or "\r" in detail:
                continue
            item = row["itemid"]
            line = (
                f"{time} | {label} | {items.get(item, item)} | stay_id={row['stay_id']} | {detail}"
            )
            event = time, line
            group = row["stay_id"], item, row.get("statusdescription", "")
            first, last = series[key].get(group, (event, event))
            series[key][group] = min(first, event), max(last, event)
        for key, by_item in series.items():
            newest = sorted(by_item.values(), key=lambda pair: pair[1])[-cap:]
            groups[key][f"icu/{table}"] = sorted({event for pair in newest for event in pair})


def during(row, admission, event, recorded):
    start, end = admission["admittime"], admission["dischtime"]
    return bool(start <= row[event] <= row[recorded] <= end)


def linked(root, table, admissions):
    for row in rows(root, table, keys=admissions):
        yield pair(row), row


def compact(row, *columns):
    return " | ".join(f"{key}={row[key]}" for key in columns if row[key])


def add_groups(hosp, notes, admissions, groups):
    for table, kind, columns in (
        ("services", "Servizio", ("transfertime", "prev_service", "curr_service")),
        ("transfers", "Trasferimento", ("transfer_id", "eventtype", "careunit", "intime")),
    ):
        for key, row in linked(hosp, table, admissions):
            time = row["transfertime" if table == "services" else "intime"]
            if admissions[key]["admittime"] <= time <= admissions[key]["dischtime"]:
                groups[key][table].append((time, f"{time} | {kind} | {compact(row, *columns)}"))

    for table, dictionary, label in (
        ("diagnoses_icd", "d_icd_diagnoses", "Diagnosi ICD codificata retrospettivamente"),
        ("procedures_icd", "d_icd_procedures", "Procedura ICD codificata retrospettivamente"),
    ):
        titles = {
            (row["icd_code"], row["icd_version"]): row["long_title"]
            for row in rows(hosp, dictionary)
        }
        for key, row in linked(hosp, table, admissions):
            code, version = row["icd_code"], row["icd_version"]
            date = row.get("chartdate", "")
            if date and not (
                admissions[key]["admittime"][:10] <= date <= admissions[key]["dischtime"][:10]
            ):
                continue
            title = titles.get((code, version), "")
            line = f"{label} | ICD-{version}: {code} | {title} | seq_num={row['seq_num']}"
            groups[key][table].append((int(row["seq_num"]), f"{date} | {line}" if date else line))

    add_labs(hosp, admissions, groups)
    add_medications(hosp, admissions, groups)
    add_documented_events(hosp, admissions, groups)

    for key, row in linked(notes, "radiology", admissions):
        if during(row, admissions[key], "charttime", "storetime") and (
            0 < len(row["text"].strip()) <= 10000
        ):
            groups[key]["radiology"].append(
                (row["storetime"], row["charttime"], row["note_id"], row["text"])
            )


def add_labs(hosp, admissions, groups):
    items = {row["itemid"]: row for row in rows(hosp, "d_labitems")}
    labs = defaultdict(lambda: defaultdict(list))
    for key, row in linked(hosp, "labevents", admissions):
        value = row["value"] or row["valuenum"]
        if not value or not during(row, admissions[key], "charttime", "storetime"):
            continue
        item = items.get(row["itemid"], {})
        rank = row["charttime"], row["storetime"], row["labevent_id"]
        label = item.get("label", row["itemid"])
        line = f"{row['charttime']} | {label}: {value} {row['valueuom']} | "
        line += compact(row, "labevent_id", "itemid", "specimen_id", "storetime", "flag")
        if item.get("fluid"):
            line += f" | campione={item['fluid']}"
        series = labs[key][row["itemid"]]
        series.append((rank, line))
        series.sort()
        if len(series) > 3:
            del series[1]  # Prima misura e ultime due per lo stesso item.
    for key, by_item in labs.items():
        series = sorted(by_item.values(), key=lambda entries: entries[-1])[-60:]
        groups[key]["labevents"] = [event for item in series for event in item]


def add_medications(hosp, admissions, groups):
    pharmacy = {}
    for key, row in linked(hosp, "pharmacy", admissions):
        if admissions[key]["admittime"] <= row["entertime"] <= admissions[key]["dischtime"]:
            pharmacy[key, row["pharmacy_id"]] = row

    orders = defaultdict(dict)
    discharge = defaultdict(set)
    for key, row in linked(hosp, "prescriptions", admissions):
        start, drug = row["starttime"], row["drug"]
        if not drug:
            continue
        order_id = row["pharmacy_id"]
        dispensing = pharmacy.get((key, order_id), {})
        details = compact(
            row,
            "pharmacy_id",
            "poe_id",
            "drug",
            "dose_val_rx",
            "dose_unit_rx",
            "route",
            "doses_per_24_hrs",
        )
        if dispensing.get("proc_type") == "Discharge Med":
            if dispensing["status"] != "Active" or not during(
                dispensing, admissions[key], "entertime", "verifiedtime"
            ):
                continue
            when = dispensing["verifiedtime"]
            details += " | " + compact(
                dispensing,
                "proc_type",
                "status",
                "entertime",
                "verifiedtime",
                "frequency",
                "duration",
                "duration_interval",
            )
            discharge[key].add((when, f"{when} | Ordine di farmacia per dimissione | {details}"))
            continue
        if admissions[key]["admittime"] <= start <= admissions[key]["dischtime"]:
            if dispensing:
                details += " | " + compact(dispensing, "proc_type", "status", "entertime")
            label = drug, row["route"]
            order = start, f"{start} | Ordine farmacologico in degenza | {details}"
            orders[key][label] = max(orders[key].get(label, order), order)
    for key, by_drug in orders.items():
        groups[key]["prescriptions"] = sorted(by_drug.values())[-40:]
    for key, events in discharge.items():
        groups[key]["pharmacy + hosp/prescriptions (Discharge Med)"] = sorted(events)[-40:]

    administrations = defaultdict(dict)
    for key, row in linked(hosp, "emar", admissions):
        if not row["medication"] or not during(row, admissions[key], "charttime", "storetime"):
            continue
        label = row["medication"], row["event_txt"]
        rank = row["charttime"], row["storetime"], row["emar_id"]
        previous = administrations[key].get(label)
        if previous is None or rank > previous[0]:
            administrations[key][label] = rank, row

    selected = {
        (key[0], row["emar_id"]): (key, row)
        for key, entries in administrations.items()
        for _, row in sorted(entries.values(), key=lambda entry: entry[0])[-64:]
    }
    details = defaultdict(set)
    if selected:
        for row in rows(hosp, "emar_detail", keys=selected, key_columns=("subject_id", "emar_id")):
            detail = compact(
                row,
                "administration_type",
                "dose_given",
                "dose_given_unit",
                "complete_dose_not_given",
                "route",
                "infusion_rate",
                "infusion_rate_unit",
            )
            if detail:
                details[row["subject_id"], row["emar_id"]].add(detail)
    for event_id, (key, row) in selected.items():
        time = row["charttime"]
        line = compact(row, "emar_id", "pharmacy_id", "medication", "event_txt", "storetime")
        extras = sorted(details[event_id])[:4]
        groups[key]["emar + hosp/emar_detail"].append(
            (time, " | ".join([time, "Evento registrato eMAR", line, *extras]))
        )


def add_documented_events(hosp, admissions, groups):
    microbiology = defaultdict(list)
    for key, row in linked(hosp, "microbiologyevents", admissions):
        admission = admissions[key]
        if row["charttime"] and row["storetime"]:
            available = during(row, admission, "charttime", "storetime")
        else:
            # Senza orario, escludi i giorni di confine per evitare attribuzioni tardive.
            available = (
                admission["admittime"][:10]
                < row["chartdate"]
                <= row["storedate"]
                < admission["dischtime"][:10]
            )
        if not available:
            continue
        time = row["charttime"] or row["chartdate"]
        result = compact(
            row,
            "microevent_id",
            "micro_specimen_id",
            "storetime",
            "storedate",
            "spec_type_desc",
            "test_name",
            "org_name",
            "quantity",
            "ab_name",
            "interpretation",
        )
        microbiology[key].append((time, f"{time} | Risultato di microbiologia | {result}"))
    for key, events in microbiology.items():
        groups[key]["microbiologyevents"] = sorted(events)[-48:]

    orders = defaultdict(dict)
    for key, row in linked(hosp, "poe", admissions):
        time = row["ordertime"]
        if not admissions[key]["admittime"] <= time <= admissions[key]["dischtime"]:
            continue
        label = (
            row["order_type"],
            row["order_subtype"],
            row["transaction_type"],
            row["order_status"],
        )
        rank = time, row["poe_id"]
        previous = orders[key].get(label)
        if previous is None or rank > previous[0]:
            orders[key][label] = rank, row

    selected = {
        (key[0], row["poe_id"]): (key, row)
        for key, entries in orders.items()
        for _, row in sorted(entries.values(), key=lambda entry: entry[0])[-40:]
    }
    details = defaultdict(set)
    if selected:
        for row in rows(hosp, "poe_detail", keys=selected, key_columns=("subject_id", "poe_id")):
            value = row["field_value"]
            if value and len(value) <= 240 and "\n" not in value and "\r" not in value:
                details[row["subject_id"], row["poe_id"]].add(
                    compact(row, "field_name", "field_value")
                )
    for order_id, (key, row) in selected.items():
        time = row["ordertime"]
        fields = compact(
            row,
            "poe_id",
            "order_type",
            "order_subtype",
            "transaction_type",
            "order_status",
            "discontinue_of_poe_id",
        )
        extras = sorted(details[order_id])[:4]
        groups[key]["poe + hosp/poe_detail"].append(
            (time, " | ".join([time, "Ordine POE", fields, *extras]))
        )

    codes = {row["code"]: row for row in rows(hosp, "d_hcpcs")}
    for key, row in linked(hosp, "hcpcsevents", admissions):
        date, code = row["chartdate"], row["hcpcs_cd"]
        if admissions[key]["admittime"][:10] <= date <= admissions[key]["dischtime"][:10]:
            description = row["short_description"] or codes.get(code, {}).get(
                "short_description", ""
            )
            groups[key]["hcpcsevents"].append(
                (date, f"{date} | Prestazione HCPCS | {code} | {description}")
            )
    for key, row in linked(hosp, "drgcodes", admissions):
        line = compact(row, "drg_type", "drg_code", "description")
        groups[key]["drgcodes"].append(
            (row["drg_type"], f"Raggruppamento amministrativo DRG | {line}")
        )


def load_mimic(hosp: Path, notes: Path, *, limit: int, seed: int, ed=None, icu=None):
    if not 1 <= limit <= MAX_CASES or seed < 0:
        raise ValueError(f"Usare da 1 a {MAX_CASES} ricoveri e un seed non negativo.")
    if (ed is None) != (icu is None):
        raise ValueError("Per il campione misto fornire entrambe le cartelle ED e ICU.")
    if ed is not None:
        all_admissions = {
            pair(row): {
                field: row[field]
                for field in (
                    "admittime",
                    "dischtime",
                    "admission_type",
                    "admission_location",
                    "discharge_location",
                )
            }
            for row in rows(hosp, "admissions")
            if row["admittime"] and row["admittime"] < row["dischtime"]
        }
        ed_stays = index_stays(ed, "edstays", all_admissions)
        icu_stays = index_stays(icu, "icustays", all_admissions)
        references = mixed_cohort(
            hosp, notes, all_admissions, ed_stays, icu_stays, limit=limit, seed=seed
        )
        admissions = {key: all_admissions[key] for key in references}
        ed_stays = {id_: entry for id_, entry in ed_stays.items() if entry[0] in admissions}
        icu_stays = {id_: entry for id_, entry in icu_stays.items() if entry[0] in admissions}
        del all_admissions
    else:
        references = cohort(hosp, notes, limit=limit, seed=seed)
        admissions = {pair(row): row for row in rows(hosp, "admissions") if pair(row) in references}
    subjects = {key[0] for key in admissions}
    patients = {
        row["subject_id"]: row for row in rows(hosp, "patients") if row["subject_id"] in subjects
    }
    if set(admissions) != set(references) or set(patients) != subjects:
        raise ValueError("Associazione paziente e ricovero incompleta.")

    groups = defaultdict(lambda: defaultdict(list))
    add_groups(hosp, notes, admissions, groups)
    if ed is not None:
        add_ed(ed, ed_stays, admissions, groups)
        add_icu(icu, icu_stays, admissions, groups)
    prepared = {}
    for key, admission in sorted(admissions.items()):
        subject, hadm = key
        header = (
            f"subject_id={subject} | hadm_id={hadm}\n"
            + compact(patients[subject], "gender", "anchor_age", "anchor_year")
            + "\n"
            + compact(admission, "admittime", "admission_type", "admission_location")
            + f"\nData effettiva di dimissione registrata: {admission['dischtime']}\n"
            + compact(admission, "discharge_location")
        )
        documents = [("contesto", "hosp/admissions + hosp/patients", header)]
        for table, entries in sorted(groups[key].items()):
            if table == "radiology":
                for stored, chart, note_id, text in sorted(entries)[-6:]:
                    documents.append(
                        (
                            "referto_radiologico",
                            f"note/radiology/{note_id}",
                            f"charttime={chart} | storetime={stored}\n\n{text}",
                        )
                    )
            else:
                documents.append(
                    (
                        "dati_ricovero",
                        (table if table.startswith(("ed/", "icu/")) else f"hosp/{table}"),
                        f"MIMIC-IV {table}\n" + "\n".join(entry[-1] for entry in sorted(entries)),
                    )
                )
        prepared[key] = documents

    # L'impronta lega campione, estrazione, fonti e versioni delle lettere.
    digest = hashlib.sha256(
        json.dumps(
            [
                MIXED_EXTRACTION if ed is not None else EXTRACTION,
                seed,
                list(prepared.items()),
                list(sorted(references.items())),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    contexts, sources, targets = [], [], {}
    for (subject, hadm), documents in prepared.items():
        context_id = f"mimic-{hadm}"
        contexts.append(
            Context(
                id=context_id,
                dataset=MIMIC_DATASET,
                dataset_version=digest,
                scope="episodio",
                patient_id=subject,
                episode_id=hadm,
                limitations=(
                    "Dati MIMIC-IV deidentificati; date traslate e anchor_age riferita "
                    "ad anchor_year. "
                    "Non ricostruire la data di nascita. Admission_type/location indicano il tipo "
                    "e la provenienza amministrativa, non il motivo clinico.",
                    "Campione retrospettivo. I codici ICD di ricovero sono codifica amministrativa "
                    "post-hoc e vanno distinti da diagnosi cliniche narrative. "
                    "La destinazione amministrativa non descrive le condizioni cliniche.",
                    "Fonti selezionate: fino a 60 esami con prima misura e ultime due; eventi "
                    "eMAR, microbiologia, ordini POE, farmaci, codici amministrativi e referti. "
                    "Per eMAR, conservare lo stato documentato. Gli ordini di reparto non "
                    "attestano somministrazione o prescrizione finale.",
                    "Gli ordini di dimissione espliciti provengono da Discharge Med attivi e "
                    "verificati entro l'uscita, abbinati a prescriptions. Sono singoli ordini "
                    "documentati e possono coprire solo una parte della terapia finale.",
                    "La lettera originale è riservata al confronto. Terapia finale, istruzioni, "
                    "follow-up e stato istologico possono mancare nelle fonti.",
                ),
            )
        )
        for number, (kind, origin, text) in enumerate(documents, 1):
            sources.append(
                EvidenceSource(
                    id=f"{context_id}-fonte-{number:03}",
                    context_id=context_id,
                    kind=kind,
                    text=text,
                    origin=origin,
                    version=digest,
                    sha256=hashlib.sha256(text.encode()).hexdigest(),
                )
            )
        targets[context_id] = references[subject, hadm]
    return contexts, sources, targets
