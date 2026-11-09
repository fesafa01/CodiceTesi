"""Campione MIMIC-IV Hosp con lettere di riferimento separate dalle fonti."""

import csv
import gzip
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from itertools import batched
from pathlib import Path

from codice_tesi.discharge.models import (
    MIMIC_DATASET,
    MIMIC_DISCHARGE_ORDERS,
    MIMIC_FOLLOWUP_ORDERS,
    Context,
    EvidenceSource,
)

EXTRACTION = "hosp-note-2.2-expanded-v3"
MAX_CASES = 100
MIXED_EXTRACTION = "hosp-note-ed-icu-2.2-v2"
RICH_EXTRACTION = "hosp-note-ed-icu-2.2-rich-v1"
HOSP_ED_EXTRACTION = "hosp-note-ed-2.2-v1"
MIXED_PATHS = ((True, True), (True, False), (False, True), (False, False))
DISCHARGE_ORDERS = MIMIC_DISCHARGE_ORDERS
POE_ORDERS = "poe + hosp/poe_detail"
FOLLOWUP_ORDERS = MIMIC_FOLLOWUP_ORDERS
FOLLOWUP_SIGNAL = re.compile(r"\b(?:follow[\s-]*up|post[\s-]*discharge|after discharge)\b", re.I)
INTENSIVE_CARE_UNIT = re.compile(
    r"\b(?:intensive care(?: unit)?|coronary care unit|critical care unit|"
    r"icu|micu|sicu|nicu|picu|ticu|cvicu|ccu|csru)\b",
    re.I,
)
logger = logging.getLogger(__name__)


def rows(root: Path, table: str, *, keys=None, key_columns=("subject_id", "hadm_id"), match=None):
    path = root / f"{table}.csv.gz"
    with gzip.open(path, "rt", encoding="utf-8", newline="") as file:
        records = csv.reader(file)
        columns = next(records, [])
        if not columns:
            raise ValueError(f"Intestazione CSV mancante: {table}.")
        if match is not None:
            column, value = match
            index = columns.index(column)
            records = (record for record in records if record[index] == value)
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


def index_stays(root, table, admissions, *, linked_admissions=None):
    stays = {}
    for row in rows(root, table, keys=admissions):
        key = pair(row)
        if linked_admissions is not None:
            linked_admissions.add(key)
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


def mixed_eligibility(hosp, notes, admissions):
    """Raccoglie gli abbinamenti di riferimento, il primo servizio e la diagnosi principale."""
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
    return services, diagnosis


def hosp_ed_cohort(hosp, notes, ed, admissions, ed_stays, *, limit, seed, rank_all=False):
    """Seleziona ricoveri senza ICU, con lettera e fonti Hosp utilizzabili."""
    from codice_tesi.backend.mimic_coverage import clinical_coverage

    services, diagnosis = mixed_eligibility(hosp, notes, admissions)
    candidates = {key: admissions[key] for key in services.keys() & diagnosis.keys()}
    complete_medication = set()
    discharge = find_discharge_cases(hosp, candidates, complete_medication=complete_medication)
    poe, followup, detailed_poe = hosp_ed_poe_coverage(hosp, candidates, discharge)
    ed_keys = {key for key, _ in ed_stays.values()}
    strata = {
        has_ed: {key for key in poe if (key in ed_keys) == has_ed} for has_ed in (False, True)
    }
    quota = limit // 2
    patients_by_path = {has_ed: {subject for subject, _ in keys} for has_ed, keys in strata.items()}
    unique_patients = len(patients_by_path[False] | patients_by_path[True])
    if (
        any(len(subjects) < quota for subjects in patients_by_path.values())
        or unique_patients < limit
    ):
        raise ValueError(
            f"Non ci sono {quota} pazienti distinti per percorso con lettera, "
            "Discharge Med utilizzabile e ordini POE in degenza."
        )
    coverage = clinical_coverage(
        hosp, notes, ed, ed_stays, {key: admissions[key] for key in poe}, extended=True
    )
    scores = Counter(key for keys in coverage.values() for key in keys)
    for keys, weight in ((complete_medication, 3), (followup, 3), (detailed_poe, 1)):
        for key in keys & poe:
            scores[key] += weight
    ranks = {key: hashlib.sha256(f"{seed}:{key[0]}:{key[1]}".encode()).digest() for key in poe}
    if rank_all:
        return sorted(poe, key=lambda key: (-scores[key], ranks[key], key))
    services_used, diagnoses_used = Counter(), Counter()
    selected, patients = [], set()
    for remaining in range(quota, 0, -1):
        for has_ed in (False, True):
            other_patients = patients_by_path[not has_ed] - patients
            other_quota = remaining if not has_ed else remaining - 1
            choices = (
                key
                for key in strata[has_ed]
                if key[0] not in patients
                and (len(other_patients) > other_quota or key[0] not in other_patients)
            )
            key = min(
                choices,
                key=lambda k: (
                    -scores[k],
                    services_used[services[k][1]],
                    diagnoses_used[diagnosis[k]],
                    ranks[k],
                    k,
                ),
            )
            selected.append(key)
            patients.add(key[0])
            services_used[services[key][1]] += 1
            diagnoses_used[diagnosis[key]] += 1

    references = selected_references(notes, set(selected))
    return {key: references[key] for key in selected}


def hosp_ed_poe_coverage(hosp, admissions, discharge):
    """Conta il contenuto documentato, con un solo bonus per categoria e ricovero."""
    poe, followup, detailed = set(), set(), set()
    active = {}
    for key, row in linked(hosp, "poe", discharge):
        if not admissions[key]["admittime"] <= row["ordertime"] <= admissions[key]["dischtime"]:
            continue
        poe.add(key)
        if followup_order_exclusion(row, admissions[key]) is None:
            active[key[0], row["poe_id"]] = key
            if followup_signals(row, ()):
                followup.add(key)
    if active:
        for row in rows(hosp, "poe_detail", keys=active, key_columns=("subject_id", "poe_id")):
            value = row["field_value"].strip()
            if not value or not value.replace("_", "").strip():
                continue
            key = active[row["subject_id"], row["poe_id"]]
            if row["field_name"].casefold() in {
                "indication",
                "consult status",
                "discharge planning",
            }:
                detailed.add(key)
            if FOLLOWUP_SIGNAL.search(" ".join(value.split())):
                followup.add(key)
    return poe, followup, detailed


def mixed_cohort(
    hosp,
    notes,
    admissions,
    ed_stays,
    icu_stays,
    *,
    limit,
    seed,
    min_discharge_cases=0,
    min_complete_cases=0,
    rich_pilot=False,
    ed=None,
):
    """Bilancia i percorsi di cura e privilegia servizi e categorie ICD diversi."""
    discharge_cases = set()
    required_discharge = max(min_discharge_cases, min_complete_cases)
    if required_discharge or rich_pilot:
        logger.info("Ricerca degli ordini di dimissione nei CSV Hosp.")
        discharge_cases = find_discharge_cases(hosp, admissions)
        count = len({subject for subject, _ in discharge_cases})
        logger.info(
            "Pazienti con ordini di dimissione utilizzabili prima degli altri vincoli: %s.", count
        )
        if count < required_discharge:
            raise ValueError(
                f"Solo {count} pazienti con ordini di dimissione utilizzabili. "
                f"Quota richiesta: {required_discharge}."
            )

    ed_keys = {key for key, _ in ed_stays.values()}
    icu_keys = {key for key, _ in icu_stays.values()}
    services, diagnosis = mixed_eligibility(hosp, notes, admissions)
    eligible = diagnosis.keys() & services.keys()
    complete_cases = set()
    if min_complete_cases:
        candidates = discharge_cases & ed_keys & icu_keys & eligible
        if candidates:
            logger.info("Ricerca dei candidati POE nei ricoveri con terapia, ED e ICU.")
            candidate_admissions = {key: admissions[key] for key in candidates}
            orders, details = load_poe(hosp, candidate_admissions)
            complete_cases = {
                key
                for identity, (key, row) in orders.items()
                if followup_order_exclusion(row, admissions[key]) is None
                and followup_signals(row, details[identity])
            }
        count = len({subject for subject, _ in complete_cases})
        logger.info("Pazienti idonei con terapia, candidato POE, ED e ICU: %s.", count)
        if count < min_complete_cases:
            raise ValueError(
                f"Solo {count} pazienti soddisfano tutti i criteri. "
                f"Quota richiesta: {min_complete_cases}."
            )

    coverage_counts = Counter()
    if rich_pilot:
        from codice_tesi.backend.mimic_coverage import clinical_coverage

        eligible_admissions = {key: admissions[key] for key in eligible}
        coverage = clinical_coverage(hosp, notes, ed, ed_stays, eligible_admissions, extended=True)
        for keys in (*coverage.values(), discharge_cases):
            coverage_counts.update(keys)

    strata = defaultdict(list)
    for key in eligible:
        stratum = key in ed_keys, key in icu_keys
        rank = hashlib.sha256(f"{seed}:{key[0]}:{key[1]}".encode()).digest()
        strata[stratum].append((key, services[key][1], diagnosis[key], rank))

    selected, patients = set(), set()
    service_counts, diagnosis_counts = Counter(), Counter()
    covered = 0
    paths = [MIXED_PATHS[index % len(MIXED_PATHS)] for index in range(limit)]
    # Riserva prima i posti con tutti i criteri, mantenendo il numero di posti per percorso.
    for _ in range(min_complete_cases):
        paths.remove(MIXED_PATHS[0])
    paths = [MIXED_PATHS[0]] * min_complete_cases + paths
    for index, stratum in enumerate(paths):
        choices = (
            candidate
            for candidate in strata[stratum]
            if candidate[0][0] not in patients
            and (index >= min_complete_cases or candidate[0] in complete_cases)
        )
        best = min(
            choices,
            key=lambda c: (
                covered < min_discharge_cases and c[0] not in discharge_cases,
                -coverage_counts[c[0]],
                service_counts[c[1]],
                diagnosis_counts[c[2]],
                c[3],
                c[0],
            ),
            default=None,
        )
        if best is None:
            raise ValueError(f"Ricoveri con lettera insufficienti per il percorso {stratum}.")
        key, service, code, _ = best
        selected.add(key)
        patients.add(key[0])
        covered += key in discharge_cases
        service_counts[service] += 1
        diagnosis_counts[code] += 1

    if covered < min_discharge_cases:
        raise ValueError(
            f"La selezione mista ha trovato {covered}/{min_discharge_cases} ricoveri "
            "con ordini di dimissione utilizzabili. Ridurre --min-discharge-cases."
        )
    if required_discharge:
        logger.info(
            "Campione selezionato: %s/%s ricoveri con ordini di dimissione.", covered, limit
        )
    if min_complete_cases:
        logger.info(
            "Ricoveri selezionati con terapia, candidato POE, ED e ICU: %s/%s.",
            len(selected & complete_cases),
            limit,
        )

    return selected_references(notes, selected)


def selected_references(notes, selected):
    references = defaultdict(list)
    for row in rows(notes, "discharge", keys=selected):
        if row["text"].strip():
            references[pair(row)].append(row)
    for letters in references.values():
        letters.sort(key=lambda row: (row["storetime"], row["charttime"], row["note_id"]))
        if len({row["note_id"] for row in letters}) != len(letters):
            raise ValueError("Identificativi duplicati nelle lettere di riferimento.")
    return references


def add_ed(ed, stays, admissions, groups, *, full=False, clinical_details=False):
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
            extra = (
                {
                    "medrecon": ("gsn", "ndc", "etc_rn", "etccode", "etcdescription"),
                    "pyxis": ("med_rn", "gsn_rn", "gsn"),
                }.get(table, ())
                if clinical_details
                else ()
            )
            detail = compact(row, *fields, *(field for field in extra if field in row))
            if detail:
                events[key].add((time, f"{label} | stay_id={row['stay_id']} | {detail}"))
        for key, entries in events.items():
            ordered = sorted(entries)
            groups[key][f"ed/{table}"] = (
                ordered
                if full or len(ordered) <= cap
                else [*ordered[: cap // 2], *ordered[-(cap - cap // 2) :]]
            )


def add_icu(icu, stays, admissions, groups, *, full=False):
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
        full_events = defaultdict(list)
        for row in rows(icu, table, keys=stays, key_columns=("subject_id", "stay_id")):
            key, stay = stays[row["subject_id"], row["stay_id"]]
            time = row[start]
            if pair(row) != key or not (
                stay["intime"] <= time <= row[stop] <= stay["outtime"]
                and time <= row["storetime"] <= admissions[key]["dischtime"]
            ):
                continue
            detail = compact(row, *fields)
            if not detail or (not full and (len(detail) > 300 or "\n" in detail or "\r" in detail)):
                continue
            if full:
                detail = " ".join(detail.split())
            item = row["itemid"]
            line = (
                f"{time} | {label} | {items.get(item, item)} | stay_id={row['stay_id']} | {detail}"
            )
            event = time, line
            if full:
                full_events[key].append(event)
                continue
            group = row["stay_id"], item, row.get("statusdescription", "")
            first, last = series[key].get(group, (event, event))
            series[key][group] = min(first, event), max(last, event)
        for key, entries in full_events.items():
            groups[key][f"icu/{table}"] = entries
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


def add_groups(hosp, notes, admissions, groups, *, full=False, clinical_details=False):
    for table, kind, columns in (
        ("services", "Servizio", ("transfertime", "prev_service", "curr_service")),
        ("transfers", "Trasferimento", ("transfer_id", "eventtype", "careunit", "intime")),
    ):
        for key, row in linked(hosp, table, admissions):
            time = row["transfertime" if table == "services" else "intime"]
            if admissions[key]["admittime"] <= time <= admissions[key]["dischtime"]:
                extra = (
                    ("outtime",)
                    if (
                        clinical_details
                        and table == "transfers"
                        and row.get("outtime")
                        and row["outtime"] <= admissions[key]["dischtime"]
                    )
                    else ()
                )
                groups[key][table].append(
                    (time, f"{time} | {kind} | {compact(row, *columns, *extra)}")
                )

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

    add_labs(hosp, admissions, groups, full=full, clinical_details=clinical_details)
    add_medications(hosp, admissions, groups, full=full, clinical_details=clinical_details)
    add_documented_events(hosp, admissions, groups, full=full, clinical_details=clinical_details)

    for key, row in linked(notes, "radiology", admissions):
        length = len(row["text"].strip())
        if (
            length
            and (full or length <= 10000)
            and during(row, admissions[key], "charttime", "storetime")
        ):
            groups[key]["radiology"].append(
                (row["storetime"], row["charttime"], row["note_id"], row["text"])
            )


def add_labs(hosp, admissions, groups, *, full=False, clinical_details=False):
    items = {row["itemid"]: row for row in rows(hosp, "d_labitems")}
    labs = defaultdict(lambda: defaultdict(list))
    for key, row in linked(hosp, "labevents", admissions):
        value = row["value"] or row["valuenum"]
        comment = result_comment(row) if full else ""
        if not (value or comment) or not during(row, admissions[key], "charttime", "storetime"):
            continue
        item = items.get(row["itemid"], {})
        rank = row["charttime"], row["storetime"], row["labevent_id"]
        label = item.get("label", row["itemid"])
        result = f"{value} {row['valueuom']}".strip() if value else "Risultato nel commento"
        line = f"{row['charttime']} | {label}: {result} | "
        line += compact(row, "labevent_id", "itemid", "specimen_id", "storetime", "flag")
        if item.get("fluid"):
            line += f" | campione={item['fluid']}"
        if full:
            for field in ("ref_range_lower", "ref_range_upper"):
                if row.get(field):
                    line += f" | {field}={row[field]}"
        if comment:
            line += f" | commento={comment}"
        if clinical_details and row.get("priority"):
            line += f" | priority={row['priority']}"
        series = labs[key][row["itemid"]]
        series.append((rank, line))
        if not full:
            series.sort()
            if len(series) > 3:
                del series[1]  # Prima misura e ultime due per lo stesso item.
    for key, by_item in labs.items():
        series = (
            by_item.values()
            if full
            else sorted(by_item.values(), key=lambda entries: entries[-1])[-60:]
        )
        groups[key]["labevents"] = [event for item in series for event in item]


def discharge_order_exclusion(row, admission):
    if not row["pharmacy_id"]:
        return "senza_pharmacy_id"
    if row["status"] != "Active":
        return "non_attivo"
    if not row["verifiedtime"]:
        return "senza_verifica"
    if not during(row, admission, "entertime", "verifiedtime"):
        return "fuori_finestra"
    return None


def find_discharge_cases(hosp, admissions, *, complete_medication=None):
    """Applica alla selezione gli stessi criteri usati per produrre la fonte."""
    cases = set()
    needs_prescription = defaultdict(set)
    pharmacy = {}
    for row in rows(hosp, "pharmacy", keys=admissions, match=("proc_type", "Discharge Med")):
        key = pair(row)
        if discharge_order_exclusion(row, admissions[key]) is not None:
            continue
        if row.get("medication", "").strip():
            cases.add(key)
        else:
            needs_prescription[key].add(row["pharmacy_id"])
        if complete_medication is not None:
            pharmacy[key, row["pharmacy_id"]] = row

    pending = {key for key, _ in pharmacy} if pharmacy else needs_prescription.keys() - cases
    if pending:
        for key, row in linked(hosp, "prescriptions", pending):
            if not row["drug"].strip():
                continue
            if row["pharmacy_id"] in needs_prescription.get(key, ()):
                cases.add(key)
            if order := pharmacy.get((key, row["pharmacy_id"])):
                fields = discharge_order_fields(order, row)
                if (
                    all(
                        (fields[name] or "").strip()
                        for name in ("dose_val_rx", "dose_unit_rx", "route")
                    )
                    and (fields["frequency"] or fields["doses_per_24_hrs"] or "").strip()
                ):
                    complete_medication.add(key)
    return cases


def discharge_order_fields(row, prescription=None, *, clinical_details=False):
    prescription = prescription or {}
    return {
        "pharmacy_id": row["pharmacy_id"],
        "poe_id": row.get("poe_id") or prescription.get("poe_id"),
        "drug": prescription.get("drug") or row.get("medication"),
        "dose_val_rx": prescription.get("dose_val_rx"),
        "dose_unit_rx": prescription.get("dose_unit_rx"),
        "route": prescription.get("route") or row.get("route"),
        "doses_per_24_hrs": prescription.get("doses_per_24_hrs") or row.get("doses_per_24_hrs"),
        **{
            name: row.get(name)
            for name in (
                "proc_type",
                "status",
                "entertime",
                "verifiedtime",
                "frequency",
                "duration",
                "duration_interval",
                "disp_sched",
            )
        },
        **(
            {
                **{
                    name: prescription.get(name)
                    for name in (
                        "starttime",
                        "stoptime",
                        "drug_type",
                        "prod_strength",
                        "form_rx",
                        "gsn",
                        "ndc",
                    )
                },
                "pharmacy_starttime": row.get("starttime"),
                "pharmacy_stoptime": row.get("stoptime"),
            }
            if clinical_details
            else {}
        ),
    }


def discharge_order_line(row, prescription=None, *, clinical_details=False):
    fields = discharge_order_fields(row, prescription, clinical_details=clinical_details)
    time = row["verifiedtime"]
    return time, f"{time} | Ordine di farmacia per dimissione | {compact(fields, *fields)}"


def add_medications(hosp, admissions, groups, *, full=False, clinical_details=False):
    pharmacy = {}
    for key, row in linked(hosp, "pharmacy", admissions):
        if row["pharmacy_id"] and (
            row["proc_type"] == "Discharge Med"
            or admissions[key]["admittime"] <= row["entertime"] <= admissions[key]["dischtime"]
        ):
            pharmacy[key, row["pharmacy_id"]] = row

    orders = defaultdict(dict)
    discharge = defaultdict(set)
    linked_discharge = set()
    for key, row in linked(hosp, "prescriptions", admissions):
        start, drug = row["starttime"], row["drug"]
        if not drug.strip():
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
        if clinical_details:
            extra = ("stoptime", "drug_type", "prod_strength", "form_rx", "gsn", "ndc")
            addition = compact(row, *(name for name in extra if name in row))
            if addition:
                details += " | " + addition
        if dispensing.get("proc_type") == "Discharge Med":
            if discharge_order_exclusion(dispensing, admissions[key]) is None:
                discharge[key].add(
                    discharge_order_line(dispensing, row, clinical_details=clinical_details)
                )
                linked_discharge.add((key, order_id))
            continue
        if admissions[key]["admittime"] <= start <= admissions[key]["dischtime"]:
            if dispensing:
                details += " | " + compact(dispensing, "proc_type", "status", "entertime")
            order = start, f"{start} | Ordine farmacologico in degenza | {details}"
            label = order if full else (drug, row["route"])
            orders[key][label] = max(orders[key].get(label, order), order)
    for key, by_drug in orders.items():
        ordered = sorted(by_drug.values())
        groups[key]["prescriptions"] = ordered if full else ordered[-40:]
    for (key, order_id), row in pharmacy.items():
        if (
            row["proc_type"] == "Discharge Med"
            and (key, order_id) not in linked_discharge
            and (row.get("medication") or "").strip()
            and discharge_order_exclusion(row, admissions[key]) is None
        ):
            discharge[key].add(discharge_order_line(row, clinical_details=clinical_details))
    for key, events in discharge.items():
        groups[key][DISCHARGE_ORDERS] = sorted(events)

    administrations = defaultdict(dict)
    for key, row in linked(hosp, "emar", admissions):
        if not row["medication"] or not during(row, admissions[key], "charttime", "storetime"):
            continue
        label = (row["emar_id"],) if full else (row["medication"], row["event_txt"])
        rank = row["charttime"], row["storetime"], row["emar_id"]
        previous = administrations[key].get(label)
        if previous is None or rank > previous[0]:
            administrations[key][label] = rank, row

    selected = {}
    for key, entries in administrations.items():
        ordered = sorted(entries.values(), key=lambda entry: entry[0])
        for _, row in ordered if full else ordered[-64:]:
            selected[key[0], row["emar_id"]] = key, row
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
            if clinical_details:
                extra = (
                    "parent_field_ordinal",
                    "pharmacy_id",
                    "dose_due",
                    "dose_due_unit",
                    "product_description",
                    "product_description_other",
                    "prior_infusion_rate",
                    "infusion_rate_adjustment",
                    "infusion_rate_adjustment_amount",
                    "infusion_complete",
                    "site",
                    "side",
                )
                addition = compact(row, *(name for name in extra if name in row))
                if addition:
                    detail += " | " + addition
            if detail:
                details[row["subject_id"], row["emar_id"]].add(detail)
    for event_id, (key, row) in selected.items():
        time = row["charttime"]
        line = compact(row, "emar_id", "pharmacy_id", "medication", "event_txt", "storetime")
        if clinical_details:
            line += " | " + compact(
                row, *(name for name in ("poe_id", "scheduletime") if name in row)
            )
        extras = sorted(details[event_id])
        if not full:
            extras = extras[:4]
        groups[key]["emar + hosp/emar_detail"].append(
            (time, " | ".join([time, "Evento registrato eMAR", line, *extras]))
        )


def load_poe(hosp, admissions):
    orders = {(key[0], row["poe_id"]): (key, row) for key, row in linked(hosp, "poe", admissions)}
    details = defaultdict(set)
    if orders:
        for row in rows(hosp, "poe_detail", keys=orders, key_columns=("subject_id", "poe_id")):
            if row["field_value"].strip():
                details[row["subject_id"], row["poe_id"]].add(
                    compact(row, "field_name", "field_value")
                )
    return orders, details


def followup_signals(row, details):
    return {
        part
        for part in (compact(row, "order_type"), compact(row, "order_subtype"), *details)
        if FOLLOWUP_SIGNAL.search(" ".join(part.split()))
    }


def followup_order_exclusion(row, admission):
    if not admission["admittime"] <= row["ordertime"] <= admission["dischtime"]:
        return "fuori_finestra"
    if row["order_status"] != "Active" or row["transaction_type"] == "D/C":
        return "non_attivo_o_annullato"
    return None


def poe_fields(row):
    return compact(
        row,
        "poe_id",
        "order_type",
        "order_subtype",
        "transaction_type",
        "order_status",
        "discontinue_of_poe_id",
    )


def add_poe(hosp, admissions, groups, *, full=False, clinical_details=False):
    all_orders, details = load_poe(hosp, admissions)
    if clinical_details:
        # Il collegamento inverso può puntare a un ordine successivo alla dimissione.
        # Esporlo soltanto quando anche l'ordine collegato è nella finestra ammessa.
        for key, row in all_orders.values():
            target = all_orders.get((key[0], row.get("discontinued_by_poe_id", "")))
            if (
                target
                and target[0] == key
                and (
                    admissions[key]["admittime"]
                    <= target[1]["ordertime"]
                    <= admissions[key]["dischtime"]
                )
            ):
                details[key[0], row["poe_id"]].add(f"discontinued_by_poe_id={target[1]['poe_id']}")
    orders = defaultdict(dict)
    for order_id, (key, row) in all_orders.items():
        time = row["ordertime"]
        if not admissions[key]["admittime"] <= time <= admissions[key]["dischtime"]:
            continue
        if (
            followup_signals(row, details[order_id])
            and followup_order_exclusion(row, admissions[key]) is None
        ):
            extras = sorted({" ".join(detail.split()) for detail in details[order_id]})
            groups[key][FOLLOWUP_ORDERS].append(
                (time, " | ".join([time, "Ordine POE", " ".join(poe_fields(row).split()), *extras]))
            )
            continue
        label = (
            (row["poe_id"],)
            if full
            else (
                row["order_type"],
                row["order_subtype"],
                row["transaction_type"],
                row["order_status"],
            )
        )
        rank = time, row["poe_id"]
        previous = orders[key].get(label)
        if previous is None or rank > previous[0]:
            orders[key][label] = rank, row

    for key, entries in orders.items():
        ordered = sorted(entries.values(), key=lambda entry: entry[0])
        for _, row in ordered if full else ordered[-40:]:
            time = row["ordertime"]
            order_id = key[0], row["poe_id"]
            if full:
                extras = sorted({" ".join(detail.split()) for detail in details[order_id]})
            else:
                extras = sorted(
                    detail
                    for detail in details[order_id]
                    if len(detail.split("field_value=", 1)[-1]) <= 240
                    and "\n" not in detail
                    and "\r" not in detail
                )[:4]
            groups[key][POE_ORDERS].append(
                (time, " | ".join([time, "Ordine POE", poe_fields(row), *extras]))
            )


def microbiology_available(row, admission):
    if row["charttime"] and row["storetime"]:
        return during(row, admission, "charttime", "storetime")
    # Senza orario, escludi i giorni di confine per evitare attribuzioni tardive.
    return (
        admission["admittime"][:10]
        < row["chartdate"]
        <= row["storedate"]
        < admission["dischtime"][:10]
    )


def result_comment(row):
    comment = row.get("comments", "")
    return " ".join(comment.split()) if comment.strip("_ \t\r\n") else ""


def add_documented_events(hosp, admissions, groups, *, full=False, clinical_details=False):
    microbiology = defaultdict(list)
    for key, row in linked(hosp, "microbiologyevents", admissions):
        admission = admissions[key]
        if not microbiology_available(row, admission):
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
        if full:
            for field in ("test_seq", "isolate_num", "dilution_text"):
                if row.get(field):
                    result += f" | {field}={row[field]}"
            if comment := result_comment(row):
                result += f" | commento={comment}"
        if clinical_details:
            extra = ("dilution_comparison", "dilution_value", "test_itemid", "org_itemid")
            addition = compact(row, *(name for name in extra if name in row))
            if addition:
                result += " | " + addition
        microbiology[key].append((time, f"{time} | Risultato di microbiologia | {result}"))
    for key, events in microbiology.items():
        ordered = sorted(events)
        groups[key]["microbiologyevents"] = ordered if full else ordered[-48:]

    add_poe(hosp, admissions, groups, full=full, clinical_details=clinical_details)

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


def load_admissions(hosp):
    return {
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


def load_mimic(
    hosp: Path,
    notes: Path,
    *,
    limit: int,
    seed: int,
    ed=None,
    icu=None,
    min_discharge_cases=0,
    min_complete_cases=0,
    rich_pilot=False,
    hosp_ed_only=False,
    candidate_batches=False,
):
    if not 1 <= limit <= MAX_CASES or seed < 0:
        raise ValueError(f"Usare da 1 a {MAX_CASES} ricoveri e un seed non negativo.")
    if not hosp_ed_only and (ed is None) != (icu is None):
        raise ValueError("Per selezionare i percorsi fornire entrambe le cartelle ED e ICU.")
    if hosp_ed_only and (
        ed is None or icu is not None or limit % 2 or min_discharge_cases or min_complete_cases
    ):
        raise ValueError(
            "--hosp-ed-only richiede --ed e un --limit pari. "
            "Omettere --icu e le quote del campione misto."
        )
    if not 0 <= min_discharge_cases <= limit:
        raise ValueError("--min-discharge-cases deve essere compreso tra 0 e --limit.")
    max_complete = (limit + len(MIXED_PATHS) - 1) // len(MIXED_PATHS)
    if not 0 <= min_complete_cases <= max_complete:
        raise ValueError(f"--min-complete-cases deve essere compreso tra 0 e {max_complete}.")
    if (min_discharge_cases or min_complete_cases or rich_pilot) and ed is None:
        raise ValueError("I vincoli di copertura richiedono il campione misto con ED e ICU.")
    if ed is not None:
        all_admissions = load_admissions(hosp)
        ed_links = set() if hosp_ed_only else None
        if hosp_ed_only:
            logger.info(
                "Esclusione dei ricoveri con passaggi in terapia intensiva da Hosp/transfers."
            )
            for row in rows(hosp, "transfers", keys=all_admissions):
                if INTENSIVE_CARE_UNIT.search(row["careunit"]):
                    all_admissions.pop(pair(row), None)
                elif row["eventtype"] == "ed":
                    ed_links.add(pair(row))
        ed_stays = index_stays(ed, "edstays", all_admissions, linked_admissions=ed_links)
        if hosp_ed_only:
            # Un passaggio ED senza uno stay utilizzabile non entra nel gruppo solo Hosp.
            for key in ed_links - {key for key, _ in ed_stays.values()}:
                all_admissions.pop(key, None)
            selection = hosp_ed_cohort(
                hosp,
                notes,
                ed,
                all_admissions,
                ed_stays,
                limit=limit,
                seed=seed,
                rank_all=candidate_batches,
            )
            if candidate_batches:
                return prepare_candidate_batches(
                    hosp, notes, ed, all_admissions, ed_stays, selection, limit, seed
                )
            references = selection
        else:
            icu_stays = index_stays(icu, "icustays", all_admissions)
            references = mixed_cohort(
                hosp,
                notes,
                all_admissions,
                ed_stays,
                icu_stays,
                limit=limit,
                seed=seed,
                min_discharge_cases=min_discharge_cases,
                min_complete_cases=min_complete_cases,
                rich_pilot=rich_pilot,
                ed=ed,
            )
            icu_stays = {id_: entry for id_, entry in icu_stays.items() if entry[0] in references}
        admissions = {key: all_admissions[key] for key in references}
        ed_stays = {id_: entry for id_, entry in ed_stays.items() if entry[0] in admissions}
        del all_admissions
    else:
        references = cohort(hosp, notes, limit=limit, seed=seed)
        admissions = {pair(row): row for row in rows(hosp, "admissions") if pair(row) in references}
    return prepare_mimic(
        hosp,
        notes,
        admissions,
        references,
        seed=seed,
        ed=ed,
        ed_stays=ed_stays if ed is not None else None,
        icu=icu,
        icu_stays=icu_stays if icu is not None else None,
        hosp_ed_only=hosp_ed_only,
        rich_pilot=rich_pilot,
        min_discharge_cases=min_discharge_cases,
        min_complete_cases=min_complete_cases,
        balanced_limit=limit if hosp_ed_only else None,
    )


def prepare_candidate_batches(hosp, notes, ed, admissions, ed_stays, ranking, limit, seed):
    for batch in batched(ranking, 8 * limit):
        references = selected_references(notes, set(batch))
        ordered = {key: references[key] for key in batch}
        batch_admissions = {key: admissions[key] for key in batch}
        stays = {identity: entry for identity, entry in ed_stays.items() if entry[0] in ordered}
        yield prepare_mimic(
            hosp,
            notes,
            batch_admissions,
            ordered,
            seed=seed,
            ed=ed,
            ed_stays=stays,
            hosp_ed_only=True,
        )


def prepare_mimic(
    hosp,
    notes,
    admissions,
    references,
    *,
    seed,
    ed=None,
    ed_stays=None,
    icu=None,
    icu_stays=None,
    hosp_ed_only=False,
    rich_pilot=False,
    min_discharge_cases=0,
    min_complete_cases=0,
    balanced_limit=None,
    clinical_details=False,
):
    subjects = {key[0] for key in admissions}
    patients = {
        row["subject_id"]: row for row in rows(hosp, "patients") if row["subject_id"] in subjects
    }
    if set(admissions) != set(references) or set(patients) != subjects:
        raise ValueError("Associazione paziente e ricovero incompleta.")

    groups = defaultdict(lambda: defaultdict(list))
    if rich_pilot:
        logger.info("Estrazione estesa di Hosp e radiologia per %s ricoveri.", len(admissions))
    add_groups(hosp, notes, admissions, groups, full=rich_pilot, clinical_details=clinical_details)
    if sum(bool(group.get(DISCHARGE_ORDERS)) for group in groups.values()) < min_discharge_cases:
        raise ValueError("Le fonti estratte non rispettano la quota di ordini di dimissione.")
    if ed is not None:
        add_ed(ed, ed_stays, admissions, groups, full=rich_pilot, clinical_details=clinical_details)
        if not hosp_ed_only and icu is not None:
            add_icu(icu, icu_stays, admissions, groups, full=rich_pilot)
    if hosp_ed_only:
        ed_count = sum(bool(groups[key].get("ed/edstays")) for key in admissions)
        if (
            balanced_limit is not None
            and (ed_count * 2 != balanced_limit or len(admissions) != balanced_limit)
        ) or any(
            not groups[key].get(DISCHARGE_ORDERS)
            or not (groups[key].get(POE_ORDERS) or groups[key].get(FOLLOWUP_ORDERS))
            for key in admissions
        ):
            raise ValueError("Le fonti estratte non rispettano i vincoli Hosp e Hosp + ED.")
    if min_complete_cases:
        complete_sources = (DISCHARGE_ORDERS, FOLLOWUP_ORDERS, "ed/edstays", "icu/icustays")
        complete_count = sum(
            all(group.get(origin) for origin in complete_sources) for group in groups.values()
        )
        if complete_count < min_complete_cases:
            raise ValueError("Le fonti estratte non rispettano la quota di casi completi.")
    display_names = {}
    if hosp_ed_only or rich_pilot:
        case_numbers = Counter()
        for key in sorted(admissions):
            available = groups[key]
            if hosp_ed_only:
                label = "Caso Hosp e ED" if available.get("ed/edstays") else "Caso Hosp"
            elif all(
                available.get(origin)
                for origin in (DISCHARGE_ORDERS, FOLLOWUP_ORDERS, "ed/edstays", "icu/icustays")
            ):
                label = "Caso completo"
            else:
                label = {
                    (True, True): "Caso ED e ICU",
                    (True, False): "Caso ED",
                    (False, True): "Caso ICU",
                    (False, False): "Caso Hosp",
                }[bool(available.get("ed/edstays")), bool(available.get("icu/icustays"))]
            case_numbers[label] += 1
            display_names[key] = (
                label
                if label == "Caso completo" and case_numbers[label] == 1
                else f"{label} {case_numbers[label]}"
            )

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
                ordered = sorted(entries)
                for stored, chart, note_id, text in ordered if rich_pilot else ordered[-6:]:
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
                (HOSP_ED_EXTRACTION, rich_pilot)
                if hosp_ed_only
                else (RICH_EXTRACTION if rich_pilot else MIXED_EXTRACTION)
                if ed is not None
                else EXTRACTION,
                seed,
                list(prepared.items()),
                list(sorted(references.items())),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    contexts, sources, targets = [], [], {}
    ordered_keys = references if hosp_ed_only and balanced_limit is None else prepared
    for subject, hadm in ordered_keys:
        documents = prepared[subject, hadm]
        context_id = f"mimic-{hadm}"
        contexts.append(
            Context(
                id=context_id,
                dataset=MIMIC_DATASET,
                dataset_version=digest,
                scope="episodio",
                patient_id=subject,
                episode_id=hadm,
                display_name=display_names.get((subject, hadm)),
                expanded_sources=rich_pilot,
                limitations=(
                    "Dati MIMIC-IV deidentificati; date traslate e anchor_age riferita "
                    "ad anchor_year. "
                    "Non ricostruire la data di nascita. Admission_type/location indicano il tipo "
                    "e la provenienza amministrativa, non il motivo clinico.",
                    "Campione retrospettivo. I codici ICD di ricovero sono codifica amministrativa "
                    "post-hoc e vanno distinti da diagnosi cliniche narrative. "
                    "La destinazione amministrativa non descrive le condizioni cliniche.",
                    (
                        "Archivio esteso degli eventi collegati e disponibili entro la dimissione."
                        if rich_pilot
                        else "Fonti selezionate: fino a 60 esami con prima misura e ultime due. "
                        "Sono inclusi eventi eMAR, microbiologia, ordini POE, farmaci, codici "
                        "amministrativi e referti."
                    ),
                    (
                        "Per eMAR, conservare lo stato documentato. Gli ordini di reparto non "
                        "attestano somministrazione o prescrizione finale."
                    ),
                    "Gli ordini di dimissione espliciti provengono da Discharge Med attivi e "
                    "verificati entro l'uscita, abbinati a prescriptions quando disponibile. "
                    "Sono singoli ordini e possono coprire solo una parte della terapia finale.",
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
    return contexts, sorted(sources, key=lambda source: source.id), targets
