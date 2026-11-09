"""Adatta soltanto i record emessi dall'adattatore MIMIC ai tool documentali.

Nessuna lettura di file, note libere o lettera di riferimento. Ogni fatto resta
collegato al passaggio originale; valori, unità e stati non vengono convertiti.
"""

import re
from datetime import datetime
from typing import TYPE_CHECKING

from codice_tesi.discharge.facts import DocumentFact, _fields

if TYPE_CHECKING:
    from codice_tesi.discharge.provenance import EvidenceCatalog

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?")
QUANTITY = re.compile(r"([<>]=?|~)?(-?\d+(?:\.\d+)?)(?: ([^;]+))?")
VITALS = {"temperature", "heartrate", "resprate", "o2sat", "sbp", "dbp", "pain"}


def _timestamp(value):
    if not value or not TIMESTAMP.fullmatch(value):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value.replace(" ", "T")


def _record_fields(parts):
    """Conserva anche attributi ripetuti e discordanti, senza sceglierne uno."""
    fields = {}
    for part in parts:
        key, sep, value = part.partition("=")
        if sep:
            if key in fields:
                previous = fields[key]
                fields[key] = (
                    [*previous, value] if isinstance(previous, list) else [previous, value]
                )
            else:
                fields[key] = value
    return fields


def _single(fields, key):
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def _quantity(value):
    match = QUANTITY.fullmatch(value)
    return {"value": match[2], "unit": match[3], "comparator": match[1]} if match else {}


def extract_mimic_facts(catalog: "EvidenceCatalog") -> tuple[DocumentFact, ...]:
    sources = {source.id: source for source in catalog.sources}
    facts = []

    def add(evidence, source, at, kind, label, fields, parts, *, status=None, role=None, **extra):
        if not at or not label:
            return
        facts.append(
            DocumentFact(
                id=f"fatto-{len(facts) + 1:04d}",
                source_id=source.id,
                evidence_id=evidence.id,
                kind=kind,
                label=label,
                event_time=at,
                status=status,
                attributes={
                    "origin": source.origin,
                    "record_fields": fields,
                    "documented_details": parts,
                    **({"record_role": role} if role else {}),
                    **extra,
                },
            )
        )

    for evidence in catalog.evidences:
        source = sources[evidence.source_id]
        if source.kind != "dati_ricovero":
            continue
        raw = source.text[evidence.start : evidence.end].strip()
        if "\n" in raw or "\r" in raw:
            continue
        parts = _fields(raw)
        if len(parts) < 2:
            continue
        fields = _record_fields(parts)
        at = _timestamp(parts[0])
        origin = source.origin
        if origin == "hosp/labevents" and at:
            label, colon, value = parts[1].partition(": ")
            if colon and value:
                add(
                    evidence,
                    source,
                    at,
                    "Observation",
                    label,
                    fields,
                    parts,
                    role="laboratory_result",
                    documented_value=value,
                    **_quantity(value),
                )
        elif origin in {"hosp/prescriptions", "hosp/pharmacy + hosp/prescriptions (Discharge Med)"}:
            expected = (
                "Ordine farmacologico in degenza"
                if origin == "hosp/prescriptions"
                else "Ordine di farmacia per dimissione"
            )
            if parts[1] == expected:
                add(
                    evidence,
                    source,
                    at,
                    "MedicationRequest",
                    _single(fields, "drug"),
                    fields,
                    parts,
                    status=_single(fields, "status"),
                    role="inpatient_order" if origin == "hosp/prescriptions" else "discharge_order",
                )
        elif origin == "hosp/emar + hosp/emar_detail" and parts[1] == "Evento registrato eMAR":
            add(
                evidence,
                source,
                at,
                "MedicationAdministration",
                _single(fields, "medication"),
                fields,
                parts,
                status=_single(fields, "event_txt"),
                role="emar_event",
            )
        elif origin == "ed/vitalsign" and parts[0] == "Misure in pronto soccorso":
            at = _timestamp(_single(fields, "charttime"))
            for name in sorted(VITALS):
                value = _single(fields, name)
                if value:
                    # Non si assumono unità né si convertono temperature o pressioni.
                    add(
                        evidence,
                        source,
                        at,
                        "Observation",
                        name,
                        fields,
                        parts,
                        role="ed_vital",
                        documented_value=value,
                        **_quantity(value),
                    )
        elif origin in {"icu/chartevents", "icu/outputevents"} and len(parts) >= 3:
            expected = "Misura ICU" if origin == "icu/chartevents" else "Uscita registrata ICU"
            value = _single(fields, "value")
            if parts[1] == expected and value:
                quantity = _quantity(value)
                quantity["unit"] = _single(fields, "valueuom")
                add(
                    evidence,
                    source,
                    at,
                    "Observation",
                    parts[2],
                    fields,
                    parts,
                    role="icu_measurement",
                    documented_value=value,
                    **quantity,
                )
        elif origin in {"icu/inputevents", "icu/ingredientevents"} and len(parts) >= 3:
            expected = (
                "Evento di infusione ICU"
                if origin == "icu/inputevents"
                else "Componente dell'infusione ICU"
            )
            if parts[1] == expected:
                add(
                    evidence,
                    source,
                    at,
                    "MedicationAdministration",
                    parts[2],
                    fields,
                    parts,
                    status=_single(fields, "statusdescription"),
                    role="icu_infusion_event",
                )
        elif origin == "icu/procedureevents" and len(parts) >= 3:
            if parts[1] == "Evento procedurale ICU":
                add(
                    evidence,
                    source,
                    at,
                    "Procedure",
                    parts[2],
                    fields,
                    parts,
                    status=_single(fields, "statusdescription"),
                    role="icu_procedure_event",
                )
        # Codici amministrativi, note, POE generici, terapia riferita e prelievi
        # Pyxis restano leggibili nel catalogo, senza trasformarli in atti clinici.
    return tuple(facts)
