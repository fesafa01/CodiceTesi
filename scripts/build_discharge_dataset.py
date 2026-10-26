"""Costruisce episodi FHIR per la stesura da una nuova popolazione Synthea.

Originali immutati; le decisioni di uscita aggiunte sono scenari sperimentali
deterministici, non prescrizioni validate clinicamente. Nessun modello è chiamato.
"""

import argparse
import base64
import copy
import hashlib
import json
import random
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

SCENARIO = "dimissione"
DAY = 86_400_000
SNOMED = "http://snomed.info/sct"
FOLLOW_UP = {
    "88805009": ("cardiologico per scompenso", 14),
    "401303003": ("cardiologico", 14),
    "401314000": ("cardiologico", 14),
    "74400008": ("chirurgico", 14),
    "363406005": ("oncologico", 14),
    "109838007": ("oncologico", 14),
    "424132000": ("oncologico", 14),
    "254837009": ("oncologico", 14),
    "840539006": ("internistico", 14),
    "185086009": ("pneumologico", 14),
    "399261000": ("cardiochirurgico", 14),
    "698306007": ("nefrologico", 14),
    "698303004": ("ematologico", 14),
}
POST_TRANSPLANT = {"698306007": "161665007", "698303004": "108631000119101"}
SELECTION = {
    "min_distinct_lab_tests": 3,
    "min_result_reports": 1,
    "min_completed_procedures": 1,
    "min_clinical_days": 2,
    "min_discharge_orders_with_dosage": 1,
}
POLICY = {
    "id": SCENARIO,
    "clinical_validation": "Non effettuata; scenari per sperimentazione documentale.",
    "discharge_diagnosis": (
        "Il medico simulato conferma il problema indicato nel caso. Nei ricoveri per "
        "trapianto usa lo stato post-trapianto già registrato nell'episodio originale, "
        "senza trasformare il motivo iniziale di attesa in una diagnosi finale."
    ),
    "discharge_assessment": (
        "Il medico dello scenario dichiara condizioni compatibili con dimissione a "
        "domicilio. È una decisione fittizia aggiunta, non un esito dedotto dai parametri. "
        "Non si aggiungono normalità degli esami, autonomia o guarigione."
    ),
    "medications": (
        "Il medico simulato dispone la prosecuzione dei farmaci cronici Synthea attivi "
        "alla fine della degenza. Codice e posologia sono copiati senza completamenti; "
        "nessuna scelta, sostituzione o ottimizzazione clinica dei farmaci."
    ),
    "follow_up": (
        "Un controllo di reparto è richiesto a 14 giorni; un appuntamento amministrativo "
        "fittizio è prenotato alla stessa scadenza in metà dei casi. Un controllo dal "
        "medico curante è richiesto entro 7 giorni. Tempi e prenotazioni sono regole "
        "del caso di prova, non derivati da visite future della simulazione."
    ),
    "histology": (
        "La risposta non è aggiunta automaticamente: l'assenza di richieste istologiche "
        "nel materiale non prova una risposta negativa."
    ),
    "references": [
        "https://github.com/synthetichealth/synthea/wiki/Generic-Module-Framework:-States",
        "https://www.nice.org.uk/guidance/ng5/chapter/Recommendations",
        "https://www.nice.org.uk/guidance/cg187/chapter/recommendations",
        "https://hl7.org/fhir/R4/codesystem-medicationrequest-category.html",
        "https://hl7.org/fhir/R4/appointment.html",
    ],
}


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value):
    # L'esportatore FHIR del runtime tronca i tempi alla precisione del secondo.
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0).isoformat()


def ident(reference):
    return reference.removeprefix("urn:uuid:").rsplit("/", 1)[-1]


def label(concept):
    return concept.get("text") or "; ".join(
        c.get("display", c.get("code", "")) for c in concept.get("coding", [])
    )


def uid(*parts):
    return str(uuid5(NAMESPACE_URL, SCENARIO + "/" + "/".join(parts)))


def available_at(resource):
    event = (
        resource.get("performedDateTime")
        or resource.get("effectiveDateTime")
        or resource.get("performedPeriod", {}).get("end")
        or resource.get("effectivePeriod", {}).get("end")
    )
    if not event:
        return None
    issued = resource.get("issued")
    return issued if issued and time(issued) > time(event) else event


def tag(resource, origin, ids):
    resource.setdefault("meta", {}).setdefault("tag", []).append(
        {"system": "urn:codice-tesi:synthetic-origin", "code": origin}
    )
    resource["extension"] = resource.get("extension", []) + [
        {"url": "urn:codice-tesi:source-id", "valueString": value} for value in ids
    ]
    return resource


def document(patient, encounter, kind, at, title, text, origins):
    return tag(
        {
            "resourceType": "DocumentReference",
            "id": uid(encounter, kind),
            "status": "current",
            "subject": {"reference": "urn:uuid:" + patient},
            "context": {"encounter": [{"reference": "urn:uuid:" + encounter}]},
            "date": at,
            "description": title,
            "content": [
                {
                    "attachment": {
                        "contentType": "text/plain; charset=utf-8",
                        "title": title,
                        "data": base64.b64encode((title + "\n\n" + text).encode()).decode(),
                    }
                }
            ],
        },
        "scenario-decision" if kind in {"uscita", "terapia", "controlli"} else "derived-facts",
        origins,
    )


def event_period(resource):
    for key in ("performedPeriod", "effectivePeriod", "period"):
        if resource.get(key, {}).get("start"):
            return resource[key]["start"], resource[key].get("end")
    for key in ("performedDateTime", "effectiveDateTime", "authoredOn", "recordedDate"):
        if resource.get(key):
            return resource[key], None
    return None, None


def clinical_information(resources, encounter):
    """Misura fatti originali utilizzabili, non il numero delle note generate."""
    start, end = (time(encounter["period"][key]) for key in ("start", "end"))
    patient = ident(encounter["subject"]["reference"])
    current = []
    for resource in resources:
        ref = resource.get("encounter", resource.get("context", {})).get("reference", "")
        if ident(ref) != encounter["id"]:
            continue
        if ident(resource.get("subject", {}).get("reference", "")) != patient:
            continue
        if resource.get("status") in {"entered-in-error", "cancelled", "not-done"}:
            continue
        if any(
            t.get("system") == "urn:codice-tesi:synthetic-origin"
            for t in resource.get("meta", {}).get("tag", [])
        ):
            continue
        first, last = event_period(resource)
        if (
            not first
            or not start <= time(first) <= end
            or (last and time(last) > end)
            or (resource.get("issued") and time(resource["issued"]) > end)
        ):
            continue
        current.append(resource)
    observations = {
        r["id"]: r
        for r in current
        if r["resourceType"] == "Observation"
        and r.get("status") in {"final", "amended", "corrected"}
        and any(
            c.get("code") == "laboratory"
            for cat in r.get("category", [])
            for c in cat.get("coding", [])
        )
        and any(
            key.startswith("value")
            and value not in (None, "", {}, [])
            and (key != "valueQuantity" or value.get("value") is not None)
            for key, value in r.items()
        )
        and not r.get("dataAbsentReason")
    }
    reports = [
        r
        for r in current
        if r["resourceType"] == "DiagnosticReport"
        and r.get("status") in {"final", "amended", "corrected"}
        and any(ident(v.get("reference", "")) in observations for v in r.get("result", []))
    ]
    result_ids = {
        ident(v["reference"])
        for r in reports
        for v in r.get("result", [])
        if ident(v.get("reference", "")) in observations
    }
    results = [observations[i] for i in sorted(result_ids)]
    tests = defaultdict(set)
    for r in results:
        key = tuple((c.get("system"), c.get("code")) for c in r.get("code", {}).get("coding", []))
        tests[key or label(r.get("code", {}))].add(event_period(r)[0][:10])
    procedures = [
        r for r in current if r["resourceType"] == "Procedure" and r.get("status") == "completed"
    ]
    treatments = [
        r for r in current if r["resourceType"] in {"MedicationRequest", "MedicationAdministration"}
    ]
    days = {event_period(r)[0][:10] for r in [*results, *procedures, *treatments]}
    return {
        "lab_results": len(results),
        "distinct_lab_tests": len(tests),
        "serial_lab_tests": sum(len(dates) > 1 for dates in tests.values()),
        "result_reports": len(reports),
        "completed_procedures": len(procedures),
        "procedure_types": len({label(r.get("code", {})) for r in procedures}),
        "treatment_orders": len(treatments),
        "clinical_days": len(days),
        "original_resources": len(current),
    }


def information_gaps(metrics):
    return [
        key.removeprefix("min_")
        for key, value in SELECTION.items()
        if metrics.get(key.removeprefix("min_"), 0) < value
    ]


def discharge_diagnosis(reason, resources, encounter):
    code = POST_TRANSPLANT.get(reason["code"])
    if code:
        for r in resources:
            if (
                r["resourceType"] == "Condition"
                and ident(r.get("encounter", {}).get("reference", "")) == encounter["id"]
                and ident(r.get("subject", {}).get("reference", ""))
                == ident(encounter["subject"]["reference"])
                and any(c.get("code") == code for c in r.get("code", {}).get("coding", []))
                and not any(
                    c.get("code") in {"refuted", "entered-in-error"}
                    for c in r.get("verificationStatus", {}).get("coding", [])
                )
                and r.get("recordedDate")
                and time(encounter["period"]["start"])
                <= time(r["recordedDate"])
                <= time(encounter["period"]["end"])
            ):
                return copy.deepcopy(r["code"]), [r["id"]]
        return None, []
    return {"coding": [{"system": SNOMED, "code": reason["code"], "display": reason["display"]}]}, [
        encounter["id"]
    ]


def candidate_pool(population, seed, *, count=30, max_per_reason=8, max_covid=4):
    candidates = []
    rejected, scanned = Counter(), 0
    fhir_files = {p.stem.rsplit("_", 1)[-1]: p for p in (population / "fhir").glob("*.json")}
    for file in sorted((population / "json").glob("*.json")):
        raw = json.loads(file.read_text())
        encounters = raw.get("record", {}).get("encounters", [])
        all_meds = [m for e in encounters for m in e.get("medications", [])]
        resources = by_id = None
        for encounter in encounters:
            start, stop = encounter["start"], encounter.get("stop", 0)
            reason = encounter.get("reason", {})
            if (
                encounter.get("type") != "inpatient"
                or not 2 <= (stop - start) / DAY <= 21
                or not reason.get("code")
                or not encounter.get("ended")
                or stop < time("2016-01-01T00:00:00+00:00").timestamp() * 1000
                or stop > time("2026-10-20T00:00:00+00:00").timestamp() * 1000
                or raw["attributes"].get("deathdate", stop + 1) <= stop
            ):
                continue
            scanned += 1
            # Il registro interno conserva inizio/fine effettivi, non solo lo status finale.
            active = [
                m
                for m in all_meds
                if m.get("chronic")
                and not m.get("administration")
                and m["start"] <= stop
                and (m.get("stop", 0) == 0 or m["stop"] > stop)
            ]
            by_code = {}
            for med in sorted(active, key=lambda m: m["start"]):
                by_code[med["codes"][0]["code"]] = med
            if not by_code:
                rejected["no_active_chronic_medication"] += 1
                continue
            if resources is None:
                bundle = json.loads(fhir_files[raw["attributes"]["id"]].read_text())
                resources = [e["resource"] for e in bundle["entry"] if "resource" in e]
                by_id = {r["id"]: r for r in resources}
            fhir_encounter = by_id.get(encounter["uuid"])
            if not fhir_encounter:
                rejected["missing_fhir_encounter"] += 1
                continue
            patient = by_id[raw["attributes"]["id"]]
            at, birth = time(fhir_encounter["period"]["start"]), time(patient["birthDate"])
            if at.year - birth.year - ((at.month, at.day) < (birth.month, birth.day)) < 18:
                rejected["not_adult_at_admission"] += 1
                continue
            metrics = clinical_information(resources, fhir_encounter)
            diagnosis, diagnosis_origins = discharge_diagnosis(reason, resources, fhir_encounter)
            if diagnosis is None:
                rejected["missing_post_transplant_status"] += 1
                continue
            metrics["discharge_orders_with_dosage"] = sum(
                bool(by_id.get(m["uuid"], {}).get("dosageInstruction")) for m in by_code.values()
            )
            gaps = information_gaps(metrics)
            if gaps:
                rejected.update(gaps)
                continue
            candidates.append(
                {
                    "raw_file": file,
                    "patient_id": raw["attributes"]["id"],
                    "encounter_id": encounter["uuid"],
                    "reason": reason,
                    "start": start,
                    "stop": stop,
                    "medications": list(by_code.values()),
                    "quality": metrics,
                    "discharge_diagnosis": diagnosis,
                    "diagnosis_origins": diagnosis_origins,
                }
            )
    random.Random(seed).shuffle(candidates)

    # Privilegia ampiezza clinica e cronologia; a parità evita pannelli ripetitivi.
    def rank(c):
        q = c["quality"]
        coverage = (
            min(q["distinct_lab_tests"], 12)
            + min(q["procedure_types"], 6)
            + min(q["serial_lab_tests"], 6)
            + min(q["clinical_days"], 5)
        )
        return -coverage, q["original_resources"]

    candidates.sort(key=rank)
    selected, seen, counts = [], set(), Counter()
    audit = {
        "screened_episodes": scanned,
        "eligible_episodes": len(candidates),
        "missing_information_counts": dict(rejected),
        "requirements": SELECTION,
        "max_per_reason": max_per_reason,
        "max_covid": max_covid,
    }
    for limit in range(1, max_per_reason + 1):
        for c in candidates:
            code = c["reason"]["code"]
            cap = min(limit, max_covid) if code == "840539006" else limit
            if c["patient_id"] in seen or counts[code] >= cap:
                continue
            selected.append(c)
            seen.add(c["patient_id"])
            counts[c["reason"]["code"]] += 1
            if len(selected) == count:
                return selected, audit
    raise ValueError(
        f"Solo {len(selected)} pazienti con informazioni sufficienti e nei limiti "
        f"di composizione: ne servono {count}. Ampliare la popolazione."
    )


def medication_resources(c, by_id):
    resources, origins = [], []
    for raw in c["medications"]:
        source = by_id.get(raw["uuid"])
        if not source or source["resourceType"] != "MedicationRequest":
            continue
        # Conserva soltanto identità del farmaco e istruzioni originali. Non riutilizza
        # date di validità o forniture di un ordine storico come nuova durata terapeutica.
        item = {
            "resourceType": "MedicationRequest",
            "id": uid(c["encounter_id"], raw["uuid"]),
            "status": "active",
            "intent": "order",
            "category": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/medicationrequest-category",
                            "code": "discharge",
                            "display": "Discharge",
                        }
                    ],
                    "text": "Prescrizione di dimissione del caso simulato",
                }
            ],
            "subject": {"reference": "urn:uuid:" + c["patient_id"]},
            "encounter": {"reference": "urn:uuid:" + c["encounter_id"]},
            "authoredOn": iso(c["stop"]),
            "medicationCodeableConcept": copy.deepcopy(source["medicationCodeableConcept"]),
            "note": [
                {
                    "text": "Prosecuzione disposta nello scenario sintetico; posologia "
                    "copiata dall'ordine Synthea di origine senza integrazioni."
                }
            ],
        }
        if source.get("dosageInstruction"):
            item["dosageInstruction"] = copy.deepcopy(source["dosageInstruction"])
        tag(item, "scenario-decision", [source["id"], "policy:" + SCENARIO])
        resources.append(item)
        origins.append(source["id"])
    return resources, origins


def appointment_resources(c, number, plan_id):
    end = time(iso(c["stop"]))
    clinic, days = FOLLOW_UP.get(c["reason"]["code"], ("di reparto", 14))
    target = (end + timedelta(days=days)).replace(hour=9, minute=30, second=0, microsecond=0)
    common = {
        "subject": {"reference": "urn:uuid:" + c["patient_id"]},
        "encounter": {"reference": "urn:uuid:" + c["encounter_id"]},
    }
    request = tag(
        {
            "resourceType": "ServiceRequest",
            "id": uid(c["encounter_id"], "followup"),
            "status": "active",
            "intent": "order",
            **common,
            "authoredOn": iso(c["stop"]),
            "code": {"text": "Controllo " + clinic},
            "occurrencePeriod": {"start": iso(c["stop"]), "end": target.isoformat()},
            "basedOn": [{"reference": "urn:uuid:" + plan_id}],
        },
        "scenario-decision",
        ["policy:" + SCENARIO],
    )
    requests = [request]
    gp = copy.deepcopy(request)
    gp["id"] = uid(c["encounter_id"], "curante")
    gp["code"] = {"text": "Rivalutazione dal medico curante"}
    gp["occurrencePeriod"]["end"] = (end + timedelta(days=7)).isoformat()
    requests.append(gp)
    text = f"Richiesto controllo {clinic} entro il {target:%d/%m/%Y}. "
    if number % 2 == 0:
        appointment = tag(
            {
                "resourceType": "Appointment",
                "id": uid(c["encounter_id"], "appointment"),
                "status": "booked",
                "created": iso(c["stop"]),
                "description": "Controllo " + clinic,
                "start": target.isoformat(),
                "end": (target + timedelta(minutes=30)).isoformat(),
                "basedOn": [{"reference": "urn:uuid:" + request["id"]}],
                "supportingInformation": [{"reference": "urn:uuid:" + c["encounter_id"]}],
                "participant": [
                    {"actor": {"reference": "urn:uuid:" + c["patient_id"]}, "status": "accepted"}
                ],
                # Collegamento dell'episodio per l'estrattore; Appointment non usa subject.
                "extension": [
                    {
                        "url": "urn:codice-tesi:encounter",
                        "valueReference": {"reference": "urn:uuid:" + c["encounter_id"]},
                    }
                ],
            },
            "scenario-decision",
            ["policy:" + SCENARIO],
        )
        requests.append(appointment)
        text += (
            f"Appuntamento già prenotato per il {target:%d/%m/%Y} alle 09:30 "
            "presso l'ambulatorio del reparto dell'ospedale simulato. "
        )
    else:
        text += "Appuntamento non ancora prenotato: da concordare con il reparto. "
    text += (
        "Richiesta rivalutazione dal medico curante entro il "
        f"{end + timedelta(days=7):%d/%m/%Y}. "
        "In caso di peggioramento clinico rivolgersi ai servizi di urgenza."
    )
    return requests, text


def build_episode(c, number, bundle):
    all_resources = [e["resource"] for e in bundle["entry"] if "resource" in e]
    by_id = {r["id"]: r for r in all_resources}
    patient = by_id[c["patient_id"]]
    encounter = copy.deepcopy(by_id[c["encounter_id"]])
    start, end = time(encounter["period"]["start"]), time(encounter["period"]["end"])
    birth = time(patient["birthDate"])
    if start.year - birth.year - ((start.month, start.day) < (birth.month, birth.day)) < 18:
        raise ValueError("Caso non adulto.")
    kept = [copy.deepcopy(patient), encounter]
    allowed = {
        "Condition",
        "Observation",
        "Procedure",
        "MedicationRequest",
        "MedicationAdministration",
        "CarePlan",
        "DiagnosticReport",
        "ImagingStudy",
    }
    for resource in all_resources:
        if resource["resourceType"] in {"Organization", "Practitioner", "Medication"}:
            kept.append(copy.deepcopy(resource))
            continue
        if resource["resourceType"] not in allowed:
            continue
        ref = resource.get("encounter", resource.get("context", {})).get("reference", "")
        if ident(ref) != c["encounter_id"]:
            continue
        r = copy.deepcopy(resource)
        # Nessuna nota standard retrospettiva viene trasformata in diario di degenza.
        r.pop("presentedForm", None)
        kept.append(r)
    rx, med_origins = medication_resources(c, by_id)
    if not rx:
        raise ValueError("Nessun ordine cronico con origine FHIR verificabile.")
    kept.extend(rx)
    final_id = uid(c["encounter_id"], "final-diagnosis")
    diagnosis = tag(
        {
            "resourceType": "Condition",
            "id": final_id,
            "subject": {"reference": "urn:uuid:" + c["patient_id"]},
            "encounter": {"reference": "urn:uuid:" + c["encounter_id"]},
            "recordedDate": iso(c["stop"]),
            "verificationStatus": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                        "code": "confirmed",
                    }
                ]
            },
            "code": copy.deepcopy(c["discharge_diagnosis"]),
        },
        "scenario-decision",
        [*c["diagnosis_origins"], "policy:" + SCENARIO],
    )
    encounter["diagnosis"] = [
        {
            "condition": {"reference": "urn:uuid:" + final_id},
            "use": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/diagnosis-role",
                        "code": "discharge",
                    }
                ]
            },
            "rank": 1,
        }
    ]
    kept.append(diagnosis)
    careplan = tag(
        {
            "resourceType": "CarePlan",
            "id": uid(c["encounter_id"], "discharge-plan"),
            "status": "active",
            "intent": "plan",
            "subject": {"reference": "urn:uuid:" + c["patient_id"]},
            "encounter": {"reference": "urn:uuid:" + c["encounter_id"]},
            "created": iso(c["stop"]),
            "period": {"start": iso(c["stop"])},
            "title": "Piano medico di uscita del caso simulato",
            "category": [{"text": "Piano di dimissione"}],
            "description": "Prosecuzione della terapia cronica indicata e controlli richiesti.",
        },
        "scenario-decision",
        ["policy:" + SCENARIO],
    )
    followup, followup_text = appointment_resources(c, number, careplan["id"])
    careplan["activity"] = [
        {"reference": {"reference": "urn:uuid:" + r["id"]}}
        for r in followup
        if r["resourceType"] == "ServiceRequest"
    ]
    kept.extend([careplan, *followup])
    # Gli esami seriali rimangono tutti nei dati strutturati. Per i testi derivati
    # si usa l'ultima registrazione per tipo di esame/procedura, evitando decine
    # di copie dello stesso pannello nel contesto del modello.
    clinical_by_type = {}
    for resource in kept:
        if resource["resourceType"] in {"Procedure", "DiagnosticReport"}:
            at = available_at(resource)
            if at and start <= time(at) <= end:
                key = (resource["resourceType"], label(resource.get("code", {})))
                previous = clinical_by_type.get(key)
                if not previous or time(at) > previous[0]:
                    clinical_by_type[key] = (time(at), resource)
    clinical = [
        item[1]
        for item in sorted(
            clinical_by_type.values(), key=lambda x: (x[0], x[1]["id"]), reverse=True
        )[:12]
    ]
    admission = (
        f"Motivo del ricovero: {c['reason']['display']}.\n"
        f"Ingresso in degenza: {start:%d/%m/%Y %H:%M} UTC.\n"
        "Gli accertamenti e i trattamenti successivi sono registrati nelle fonti "
        "della presente degenza."
    )
    documents = [
        document(
            c["patient_id"],
            c["encounter_id"],
            "ingresso",
            start.isoformat(),
            "Valutazione di ingresso",
            admission,
            [c["encounter_id"]],
        )
    ]
    for i, resource in enumerate(clinical, 1):
        at = available_at(resource)
        if not at or not start <= time(at) <= end:
            continue
        text = f"Attività eseguita: {label(resource.get('code', {}))}."
        if resource.get("conclusion"):
            text += "\nConclusione: " + resource["conclusion"]
        # Non si inventano interpretazioni, esiti o risultati non presenti.
        if resource["resourceType"] == "DiagnosticReport":
            values = []
            for result in resource.get("result", []):
                obs = by_id.get(ident(result.get("reference", "")))
                if obs and (not obs.get("issued") or time(obs["issued"]) <= time(at)):
                    value = obs.get("valueQuantity", obs.get("valueCodeableConcept", {}))
                    values.append(
                        label(obs.get("code", {})) + ": " + json.dumps(value, ensure_ascii=False)
                    )
            if values:
                text += "\nRisultati:\n" + "\n".join(values)
        documents.append(
            document(
                c["patient_id"],
                c["encounter_id"],
                f"referto-{i}",
                at,
                "Registrazione clinica della degenza",
                text,
                [resource["id"]],
            )
        )
    decision = (
        f"Diagnosi alla dimissione confermata dal medico dello scenario: "
        f"{label(c['discharge_diagnosis'])}.\n"
        "Il medico dello scenario dispone la dimissione a domicilio e la "
        "prosecuzione dell'assistenza ambulatoriale.\n"
        f"Data effettiva di dimissione: {end:%d/%m/%Y}.\n"
        "Condizioni alla dimissione, secondo la valutazione del medico dello "
        "scenario: quadro clinico compatibile con dimissione a domicilio e "
        "prosecuzione delle cure in regime ambulatoriale. "
        "Non sono specificate ulteriori caratteristiche obiettive o funzionali."
    )
    documents.append(
        document(
            c["patient_id"],
            c["encounter_id"],
            "uscita",
            end.isoformat(),
            "Decisioni mediche alla dimissione",
            decision,
            [final_id, "policy:" + SCENARIO],
        )
    )
    therapy = [
        "Il medico dello scenario prescrive alla dimissione la prosecuzione "
        "dei seguenti farmaci cronici. Le istruzioni non specificate restano da completare."
    ]
    for item in rx:
        instructions = item.get("dosageInstruction")
        therapy.append(
            label(item["medicationCodeableConcept"])
            + " | "
            + (
                json.dumps(instructions, ensure_ascii=False)
                if instructions
                else "Posologia non disponibile nell'ordine di origine."
            )
        )
    documents.append(
        document(
            c["patient_id"],
            c["encounter_id"],
            "terapia",
            end.isoformat(),
            "Prescrizioni per il domicilio",
            "\n".join(therapy),
            [r["id"] for r in rx],
        )
    )
    documents.append(
        document(
            c["patient_id"],
            c["encounter_id"],
            "controlli",
            end.isoformat(),
            "Controlli richiesti e prenotazioni",
            followup_text,
            [r["id"] for r in followup],
        )
    )
    kept.extend(documents)
    result = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": "urn:uuid:" + r["id"], "resource": r} for r in kept],
    }
    summary = {
        "case_id": f"caso_{number:02}",
        "patient_id": c["patient_id"],
        "encounter_id": c["encounter_id"],
        "reason": c["reason"],
        "period": encounter["period"],
        "documents": len(documents),
        "discharge_medications": len(rx),
        "booked_appointment": number % 2 == 0,
        "medication_source_ids": med_origins,
        "information_coverage": c["quality"],
        "discharge_diagnosis": c["discharge_diagnosis"],
    }
    return result, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--population",
        type=Path,
        required=True,
        help="Esportazione Synthea con cartelle fhir e json.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=2026102205)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--max-per-reason", type=int, default=8)
    parser.add_argument("--max-covid", type=int, default=4)
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Genera prima due popolazioni ufficiali da 300 pazienti.",
    )
    parser.add_argument("--java", type=Path)
    parser.add_argument("--jar", type=Path)
    args = parser.parse_args()
    if min(args.count, args.max_per_reason, args.max_covid) < 1:
        parser.error("Numero di casi e limiti per diagnosi devono essere positivi.")
    population, output = args.population.resolve(), args.output.resolve()
    if output.exists():
        raise ValueError("La cartella di destinazione deve essere nuova.")
    if args.generate:
        if not args.java or not args.jar or population.exists():
            raise ValueError("--generate richiede --java, --jar e una popolazione nuova.")
        population.mkdir(parents=True)
        runs = []
        for seed in (2026102203, 2026102206):
            argv = [
                str(args.java.resolve()),
                "-Duser.timezone=UTC",
                "-Xmx3g",
                "-jar",
                str(args.jar.resolve()),
                "-p",
                "300",
                "-s",
                str(seed),
                "-cs",
                "2026102204",
                "-r",
                "20261020",
                "-e",
                "20261020",
                "-a",
                "45-85",
                f"--exporter.baseDirectory={population}",
                "--exporter.json.export=true",
                "--exporter.years_of_history=10",
                "--exporter.fhir.use_us_core_ig=true",
                "--exporter.fhir.us_core_version=6.1.0",
                "--generate.thread_pool_size=2",
                "Massachusetts",
            ]
            log = population / f"generazione-{seed}.log"
            with log.open("w") as stream:
                subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, check=True)
            runs.append({"argv": argv, "log_sha256": sha(log)})
        dump(population / "generazione.json", {"jar_sha256": sha(args.jar), "runs": runs})
    selected, selection = candidate_pool(
        population,
        args.seed,
        count=args.count,
        max_per_reason=args.max_per_reason,
        max_covid=args.max_covid,
    )
    index, summaries, originals = [], [], {}
    fhir_files = {}
    for file in (population / "fhir").glob("*.json"):
        if file.name.startswith(("hospitalInformation", "practitionerInformation")):
            continue
        patient_id = file.stem.rsplit("_", 1)[-1]
        fhir_files[patient_id] = file
    for i, c in enumerate(selected, 1):
        source = fhir_files[c["patient_id"]]
        fhir = json.loads(source.read_text())
        bundle, summary = build_episode(c, i, fhir)
        destination = output / "episodi" / f"{summary['case_id']}.json"
        dump(destination, bundle)
        index.append(
            {
                "case_id": summary["case_id"],
                "patient_id": c["patient_id"],
                "encounter_id": c["encounter_id"],
                "fhir_file": str(destination.relative_to(output)),
                "sha256": sha(destination),
                "synthetic_enrichment": SCENARIO,
            }
        )
        for file, folder in [(source, "originali/fhir"), (c["raw_file"], "originali/json")]:
            target = output / folder / file.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            originals[str(target.relative_to(output))] = sha(target)
        summaries.append(summary)
    dump(output / "indice.json", index)
    dump(output / "config/scenario_policy.json", POLICY)
    if (population / "generazione.json").exists():
        shutil.copy2(population / "generazione.json", output / "config/generazione.json")
    for log in population.glob("generazione-*.log"):
        shutil.copy2(log, output / "config" / log.name)
    dump(
        output / "manifest.json",
        {
            "dataset_id": "synthea-dimissioni",
            "scenario_id": SCENARIO,
            "builder_sha256": sha(Path(__file__)),
            "selection_seed": args.seed,
            "eligible_episodes": selection["eligible_episodes"],
            "case_count": len(index),
            "selection": selection,
            "distinct_patients": len({r["patient_id"] for r in index}),
            "reason_distribution": dict(Counter(s["reason"]["display"] for s in summaries)),
            "clinical_validation": POLICY["clinical_validation"],
            "scenario_policy_sha256": sha(output / "config/scenario_policy.json"),
            "originals": originals,
            "cases": summaries,
        },
    )
    print(
        json.dumps(
            {
                "cases": len(index),
                "documents": sum(s["documents"] for s in summaries),
                "discharge_medications": sum(s["discharge_medications"] for s in summaries),
                "output": str(output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
