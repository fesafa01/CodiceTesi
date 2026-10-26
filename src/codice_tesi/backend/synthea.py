"""FHIR Synthea e scenari derivati → fonti della sola degenza selezionata."""

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from codice_tesi.backend.database import Database
from codice_tesi.backend.repository import save_dataset
from codice_tesi.discharge.models import Context, EvidenceSource

DATASET = "synthea-dimissioni"
EXTRACTION_VERSION = "synthea-degenza-v5"
EVENT_TYPES = {
    "Condition",
    "Observation",
    "Procedure",
    "MedicationRequest",
    "MedicationAdministration",
    "CarePlan",
    "DiagnosticReport",
    "DocumentReference",
    "ImagingStudy",
    "ServiceRequest",
    "Appointment",
}


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except ValueError:
        return None


def _id(reference):
    return reference.removeprefix("urn:uuid:").rsplit("/", 1)[-1]


def _label(concept):
    return concept.get("text") or "; ".join(
        c.get("display", c.get("code", "")) for c in concept.get("coding", [])
    )


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _time(resource):
    # Per richieste e prenotazioni conta quando l'informazione è stata registrata,
    # non quando si terrà il controllo futuro.
    if resource["resourceType"] == "ServiceRequest":
        return resource.get("authoredOn"), None
    if resource["resourceType"] == "Appointment":
        return resource.get("created"), None
    for key in ("performedPeriod", "effectivePeriod", "period"):
        if resource.get(key, {}).get("start"):
            return resource[key]["start"], resource[key].get("end")
    for key in (
        "performedDateTime",
        "effectiveDateTime",
        "authoredOn",
        "recordedDate",
        "date",
        "started",
    ):
        if resource.get(key):
            return resource[key], None
    return None, None


def _encounter_refs(resource):
    """FHIR R4 usa encounter, context.reference o context.encounter secondo la risorsa."""
    refs = [resource.get("encounter", {}).get("reference", "")]
    context = resource.get("context", {})
    refs.append(context.get("reference", ""))
    refs.extend(x.get("reference", "") for x in context.get("encounter", []))
    if resource["resourceType"] == "Appointment":
        refs.extend(
            x.get("valueReference", {}).get("reference", "")
            for x in resource.get("extension", [])
            if x.get("url") == "urn:codice-tesi:encounter"
        )
    return {_id(r) for r in refs if r}


def _recorded_during(resource, encounter, cutoff):
    first, last = _time(resource)
    at, until, issued = _date(first), _date(last), _date(resource.get("issued"))
    return bool(
        at
        and _date(encounter["period"]["start"]) <= at <= _date(encounter["period"]["end"])
        and at <= cutoff
        and (not until or until <= cutoff)
        and (not issued or issued <= cutoff)
    )


def _encounter_text(encounter, reason):
    return (
        f"Encounter {encounter['id']} | classe={encounter['class']['code']} | "
        f"periodo={_json(encounter['period'])} | inclusione={reason}"
    )


def _value(resource):
    parts = []
    for key, value in resource.items():
        if key.startswith("value"):
            if key == "valueQuantity":
                quantity = f"{value.get('comparator', '')}{value.get('value', '')}"
                unit = value.get("unit", value.get("code", ""))
                parts.append(f"{quantity} {unit}".strip())
            elif key == "valueCodeableConcept":
                parts.append(_label(value))
            else:
                parts.append(_json(value))
    for component in resource.get("component", []):
        parts.append(f"{_label(component.get('code', {}))}: {_value(component)}")
    if resource.get("dataAbsentReason"):
        parts.append("Dato assente: " + _label(resource["dataAbsentReason"]))
    if resource.get("interpretation"):
        parts.append("Interpretazione documentata: " + _json(resource["interpretation"]))
    if resource.get("referenceRange"):
        parts.append("Intervallo documentato: " + _json(resource["referenceRange"]))
    return "; ".join(parts)


def _line(resource, by_id, cutoff):
    typ = resource["resourceType"]
    first, last = _time(resource)
    code = resource.get("code", resource.get("medicationCodeableConcept", {}))
    if resource.get("medicationReference"):
        code = by_id[_id(resource["medicationReference"]["reference"])].get("code", {})
    parts = [first or "", typ, _label(code)]
    if last and _date(last) <= cutoff:
        parts.append("fine=" + last)
    if typ in {"Condition", "Procedure"}:
        parts.extend(
            f"{c.get('system', '')}: {c['code']}" for c in code.get("coding", []) if c.get("code")
        )
    if typ == "Condition":
        onset = resource.get("onsetDateTime")
        if onset:
            parts.append("insorgenza=" + onset)
        abatement = resource.get("abatementDateTime")
        if _date(abatement) and _date(abatement) <= cutoff:
            parts.append("cessazione documentata=" + abatement)
    elif typ == "Observation":
        parts.extend([resource.get("status", ""), _value(resource)])
    else:
        if resource.get("status"):
            parts.append("stato nello snapshot=" + resource["status"])
        if resource.get("category"):
            parts.append("categoria=" + "; ".join(_label(c) for c in resource["category"]))
        for key in (
            "dosageInstruction",
            "dosage",
            "activity",
            "description",
            "conclusion",
            "note",
            "occurrenceDateTime",
            "occurrencePeriod",
            "start",
            "end",
            "participant",
            "basedOn",
        ):
            if resource.get(key):
                parts.append(key + "=" + _json(resource[key]))
    return " | ".join(p for p in parts if p)


def load_synthea(path: Path) -> tuple[str, list[tuple[Context, EvidenceSource]]]:
    """Carica solo originali FHIR indicizzati e verifica identità, hash e tempi."""
    root = path.resolve()
    index_bytes = (root / "indice.json").read_bytes()
    rows = json.loads(index_bytes)
    if not isinstance(rows, list) or not rows:
        raise ValueError("Indice Synthea vuoto o non valido.")
    version = hashlib.sha256(index_bytes)
    version.update(EXTRACTION_VERSION.encode())
    bundles = []
    seen = set()
    for row in rows:
        if row["case_id"] in seen:
            raise ValueError("Caso Synthea duplicato.")
        seen.add(row["case_id"])
        file = (root / row["fhir_file"]).resolve()
        if not file.is_relative_to(root):
            raise ValueError("File FHIR fuori dalla cartella del dataset.")
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError(f"Hash FHIR non coerente: {row['case_id']}.")
        version.update(raw)
        bundles.append((row, file))
    digest = version.hexdigest()
    loaded = []
    for row, file in bundles:
        enrichment = row.get("synthetic_enrichment")
        if enrichment not in {None, "dimissione"}:
            raise ValueError("Tipo di integrazione sintetica non supportata.")
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("File FHIR modificato durante l'importazione.")
        bundle = json.loads(raw)
        if bundle.get("resourceType") != "Bundle":
            raise ValueError("Il file Synthea deve contenere un Bundle FHIR.")
        resources = [entry["resource"] for entry in bundle.get("entry", []) if "resource" in entry]
        by_id = {r["id"]: r for r in resources}
        if len(by_id) != len(resources):
            raise ValueError("Identificativi FHIR duplicati nel bundle.")
        patient, encounter = by_id[row["patient_id"]], by_id[row["encounter_id"]]
        if (
            patient["resourceType"] != "Patient"
            or encounter["resourceType"] != "Encounter"
            or _id(encounter.get("subject", {}).get("reference", "")) != patient["id"]
            or encounter.get("class", {}).get("code") != "IMP"
            or encounter.get("status") != "finished"
        ):
            raise ValueError("Identità o stato del ricovero Synthea non validi.")
        start, end = _date(encounter["period"].get("start")), _date(encounter["period"].get("end"))
        if not start or not end or start >= end:
            raise ValueError("Periodo del ricovero Synthea non valido.")
        context = Context(
            id="synthea-" + row["case_id"].replace("_", "-"),
            dataset=DATASET,
            dataset_version=digest,
            scope="episodio",
            patient_id=patient["id"],
            episode_id=encounter["id"],
            limitations=(
                (
                    "Dati Synthea con decisioni di uscita simulate da regole deterministiche; "
                    "non sottoposti a validazione clinica. Terapia cronica e posologie "
                    "derivano dagli ordini Synthea; controlli e prenotazioni sono fittizi."
                    if enrichment
                    else "Dati clinici interamente sintetici generati da Synthea; "
                    "nessuna integrazione manuale."
                ),
                "Fonti limitate alla degenza selezionata e disponibili entro la sua fine; "
                "nessun recupero automatico di pronto soccorso, altri incontri "
                "o archivio storico/CDR.",
                "I precedenti sono utilizzabili solo se riportati nella documentazione di questa "
                "degenza. Il motivo di ingresso non attesta da solo una diagnosi di dimissione.",
                "Gli stati dei farmaci/piani e le note template sono retrospettivi; "
                "non attestano da soli prescrizioni o condizioni finali.",
            ),
        )
        groups = []
        header = [
            f"{k}: {_json(patient[k])}" for k in ("name", "gender", "birthDate") if k in patient
        ]
        header.extend(
            f"{k}: {_json(encounter[k])}"
            for k in (
                "period",
                "type",
                "reasonCode",
                "serviceProvider",
                "participant",
                "hospitalization",
                "diagnosis",
            )
            if k in encounter
        )
        header.append(
            "Data effettiva di dimissione del ricovero selezionato: "
            + encounter["period"]["end"]
            + " (fine effettiva dell'Encounter di degenza con stato finished; "
            "non è una data prevista)."
        )
        groups.append(("contesto", "\n".join(header), [patient["id"], encounter["id"]]))
        notes = {}
        current = []
        for r in resources:
            verification = {
                c.get("code") for c in r.get("verificationStatus", {}).get("coding", [])
            }
            if r.get("status") == "entered-in-error" or verification & {
                "entered-in-error",
                "refuted",
            }:
                continue
            subject = r.get("subject", r.get("patient", {})).get("reference", "")
            if r["resourceType"] == "Appointment":
                patients = {
                    _id(p.get("actor", {}).get("reference", ""))
                    for p in r.get("participant", [])
                    if p.get("actor", {}).get("reference", "").startswith("Patient/")
                    or by_id.get(_id(p.get("actor", {}).get("reference", "")), {}).get(
                        "resourceType"
                    )
                    == "Patient"
                }
                subject = patient["id"] if patients == {patient["id"]} else ""
            same_patient = _id(subject) == patient["id"]
            if r["resourceType"] not in EVENT_TYPES or encounter["id"] not in _encounter_refs(r):
                continue
            if not same_patient:
                raise ValueError("Fonte FHIR collegata al ricovero ma di un altro paziente.")
            if not _recorded_during(r, encounter, end):
                continue
            attachments = []
            if r["resourceType"] == "DocumentReference":
                attachments = [c.get("attachment", {}) for c in r.get("content", [])]
            elif r["resourceType"] == "DiagnosticReport":
                attachments = r.get("presentedForm", [])
            for attachment in attachments:
                if attachment.get("data") and attachment.get("contentType", "").startswith(
                    "text/plain"
                ):
                    text = base64.b64decode(attachment["data"], validate=True).decode("utf-8")
                    notes.setdefault(text, []).append(r["id"])
            if r["resourceType"] != "DocumentReference":
                current.append(r)

        def order(r):
            return _date(_time(r)[0]), r["id"]

        episode_header = _encounter_text(encounter, "degenza selezionata")
        if current:
            groups.append(
                (
                    "dati_ricovero",
                    episode_header
                    + "\n"
                    + "\n".join(_line(r, by_id, end) for r in sorted(current, key=order)),
                    [r["id"] for r in current],
                )
            )
        for i, (text, ids) in enumerate(sorted(notes.items()), 1):
            groups.append(
                (
                    f"nota-{i}",
                    "Nota della degenza, generata da Synthea | " + episode_header + "\n\n" + text,
                    ids,
                )
            )
        for kind, text, ids in groups:
            source = EvidenceSource(
                id=f"{context.id}-{kind}",
                context_id=context.id,
                kind=kind,
                text=text,
                origin=f"{row['fhir_file']}#" + ",".join(ids),
                version=digest,
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
            loaded.append((context, source))
    return digest, loaded


async def import_synthea(
    database: Database, path: Path, *, replace: bool = False
) -> tuple[bool, int]:
    _, loaded = load_synthea(path)
    return await save_dataset(
        database,
        list({c.id: c for c, _ in loaded}.values()),
        [s for _, s in loaded],
        replace=replace,
    )
