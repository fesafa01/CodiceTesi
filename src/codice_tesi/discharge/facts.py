"""Fatti documentali estratti dai record testuali strutturati, senza inferenza clinica.

Legge la rappresentazione preparata dagli adattatori Synthea e MIMIC, anche nei
pacchetti esistenti. Le note libere non vengono interpretate automaticamente.
"""

import json
import re
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING

from pydantic import JsonValue

from codice_tesi.discharge.contracts import Contract
from codice_tesi.discharge.models import (
    MIMIC_DATASET,
    MIMIC_DISCHARGE_ORDERS,
    MIMIC_FOLLOWUP_ORDERS,
)

if TYPE_CHECKING:
    from codice_tesi.discharge.provenance import EvidenceCatalog

MIMIC_FACT_EXTRACTION_VERSION = "mimic-record-v1"

FOLLOWUP_ACTION = re.compile(
    r"\b(?:outpatient (?:follow[ -]?up|review|visit)|"
    r"(?:follow[ -]?up|appointment|visit|review) (?:with|at) \w+|"
    r"(?:controllo|visita) (?:ambulatoriale|specialistica|con|presso|di))\b",
    re.IGNORECASE,
)
POST_DISCHARGE = re.compile(
    r"\b(?:post[ -]?discharge|after (?:hospital )?discharge|dopo la dimissione)\b", re.I
)
FOLLOWUP_NEGATION = re.compile(
    r"\b(?:no (?:outpatient )?follow[ -]?up|(?:nessun[ao]?|senza) (?:controllo|visita)|"
    r"not (?:required|needed|in|within|on)|non (?:necessari[oa]|richiest[oa]|entro))\b",
    re.IGNORECASE,
)
FOLLOWUP_TIME = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a|an|"
    r"un|una|due|tre|quattro|cinque|sei|sette|otto|nove|dieci)\s+"
    r"(?:hours?|days?|weeks?|months?|ore?|giorn[oi]|settiman[ae]|mes[ei])|"
    r"next (?:week|month)|(?:la )?prossima settimana|il prossimo mese)\b",
    re.IGNORECASE,
)
FOLLOWUP_TIMING_FIELDS = {"timing", "timeframe", "time frame", "due date", "appointment date"}
TIMING_IN_INSTRUCTIONS = re.compile(
    rf"(?:\b(?:in|within|after|on|tra|fra|entro|il)\s+{FOLLOWUP_TIME.pattern}|"
    r"\b(?:next (?:week|month)|la prossima settimana|il prossimo mese)\b)",
    re.IGNORECASE,
)


class DocumentFact(Contract):
    id: str
    source_id: str
    evidence_id: str
    kind: str
    label: str
    event_time: str | None
    status: str | None
    attributes: dict[str, JsonValue]


class DocumentaryIssue(Contract):
    code: str
    message: str
    fact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    section_id: str | None = None


class FactInventory(Contract):
    schema_version: str = "1.0"
    extraction_version: str = "record-testuale-v1"
    facts: tuple[DocumentFact, ...] = ()
    issues: tuple[DocumentaryIssue, ...] = ()


def _fields(text: str) -> list[str]:
    """Separa il framing dell'adattatore, rispettando stringhe e JSON annidato."""
    parts, start, depth, quoted, escaped = [], 0, 0, False, False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif depth == 0 and text[index : index + 3] == " | ":
            parts.append(text[start:index])
            start = index + 3
    parts.append(text[start:])
    return parts


def _order_passages(catalog: "EvidenceCatalog", origin: str, label: str):
    sources = {
        source.id: source
        for source in catalog.sources
        if source.origin.removeprefix("hosp/") == origin
    }
    for evidence in catalog.evidences:
        source = sources.get(evidence.source_id)
        if source is None:
            continue
        parts = _fields(source.text[evidence.start : evidence.end].strip())
        if len(parts) >= 3 and parts[1] == label:
            yield evidence.id, parts[2:]


def _discharge_therapy_issues(catalog: "EvidenceCatalog") -> list[DocumentaryIssue]:
    issues = []
    found_order = False
    for evidence_id, parts in _order_passages(
        catalog, MIMIC_DISCHARGE_ORDERS, "Ordine di farmacia per dimissione"
    ):
        fields = {
            key: value.strip()
            for key, value in (part.split("=", 1) for part in parts if "=" in part)
        }
        drug = fields.get("drug")
        if not drug:
            continue
        found_order = True
        missing = []
        if not fields.get("dose_val_rx"):
            missing.append("dose")
        elif not fields.get("dose_unit_rx"):
            missing.append("unità della dose")
        if not fields.get("route"):
            missing.append("via di somministrazione")
        if not (fields.get("frequency") or fields.get("doses_per_24_hrs")):
            missing.append("frequenza")
        if not fields.get("duration"):
            missing.append("durata")
        if missing:
            attributes = ", ".join(missing)
            issues.append(
                DocumentaryIssue(
                    code="discharge_therapy_incomplete_order",
                    message=f"Per {drug}, verificare {attributes} nell'ordine di dimissione.",
                    fact_ids=(),
                    evidence_ids=(evidence_id,),
                    section_id="terapia_dimissione",
                )
            )
    if not found_order:
        issues.append(
            DocumentaryIssue(
                code="discharge_therapy_no_structured_orders",
                message=(
                    "Nelle fonti caricate non risultano ordini strutturati di dimissione. "
                    "Verificare la terapia prevista alla dimissione."
                ),
                fact_ids=(),
                evidence_ids=(),
                section_id="terapia_dimissione",
            )
        )
    return issues


def _followup_issues(catalog: "EvidenceCatalog") -> list[DocumentaryIssue]:
    issues = []
    found_candidate = False
    for evidence_id, parts in _order_passages(catalog, MIMIC_FOLLOWUP_ORDERS, "Ordine POE"):
        found_candidate = True
        details = [
            (
                name.removeprefix("field_name=").strip().casefold().replace("_", " "),
                value.removeprefix("field_value=").strip(),
            )
            for name, value in pairwise(parts[1:])
            if name.startswith("field_name=")
            and value.startswith("field_value=")
            and not FOLLOWUP_NEGATION.search(value.removeprefix("field_value="))
        ]
        action_details = [value for _, value in details if FOLLOWUP_ACTION.search(value)]
        post_discharge = POST_DISCHARGE.search(parts[0]) or any(
            POST_DISCHARGE.search(value) for _, value in details
        )
        timing_details = [value for name, value in details if name in FOLLOWUP_TIMING_FIELDS]
        missing = []
        if not action_details or not post_discharge:
            missing.append("indicazioni dopo la dimissione")
        if not (
            any(FOLLOWUP_TIME.search(value) for value in timing_details)
            or any(TIMING_IN_INSTRUCTIONS.search(value) for value in action_details)
        ):
            missing.append("tempistica")
        if missing:
            fields = " e ".join(missing)
            issues.append(
                DocumentaryIssue(
                    code="discharge_followup_incomplete_candidate",
                    message=f"Verificare {fields} nel passaggio del possibile follow-up.",
                    fact_ids=(),
                    evidence_ids=(evidence_id,),
                    section_id="programma_followup",
                )
            )
    if not found_candidate:
        issues.append(
            DocumentaryIssue(
                code="discharge_followup_no_structured_candidates",
                message=(
                    "Nelle fonti caricate non risultano ordini strutturati candidati al follow-up. "
                    "Verificare indicazioni e tempistiche nelle fonti disponibili."
                ),
                fact_ids=(),
                evidence_ids=(),
                section_id="programma_followup",
            )
        )
    return issues


def extract_facts(catalog: "EvidenceCatalog", *, dataset: str | None = None) -> FactInventory:
    if dataset == MIMIC_DATASET:
        from codice_tesi.discharge.mimic_facts import extract_mimic_facts

        # I rilievi già esposti nel prompt Base restano invariati. I nuovi fatti
        # alimentano i tool, senza aggiungere inferenze o nuovi rilievi al prompt.
        return FactInventory(
            extraction_version=MIMIC_FACT_EXTRACTION_VERSION,
            facts=extract_mimic_facts(catalog),
            issues=tuple(_discharge_therapy_issues(catalog) + _followup_issues(catalog)),
        )
    sources = {source.id: source for source in catalog.sources}
    facts, issues = [], []
    observations = {}
    for evidence in catalog.evidences:
        source = sources[evidence.source_id]
        if source.kind != "dati_ricovero":
            continue
        raw = source.text[evidence.start : evidence.end].strip()
        if "\n" in raw:
            continue
        parts = _fields(raw)
        if len(parts) < 3 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\S+)?", parts[0]):
            continue
        at, kind, label, *details = parts
        try:
            datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if kind not in {
            "Observation",
            "MedicationRequest",
            "MedicationAdministration",
            "Procedure",
        }:
            continue
        attributes: dict[str, JsonValue] = {"documented_details": details}
        status = None
        if kind == "Observation" and details:
            status = details[0]
            # Solo quantità semplici: pannelli, range e interpretazioni restano testo.
            if len(details) == 2:
                match = re.fullmatch(r"([<>]=?|~)?(-?\d+(?:\.\d+)?)(?: ([^;]+))?", details[1])
                if match:
                    attributes.update(value=match[2], unit=match[3], comparator=match[1])
        for detail in details:
            if detail.startswith("stato nello snapshot="):
                status = detail.partition("=")[2]
            elif detail.startswith(("dosageInstruction=", "dosage=", "categoria=")):
                key, _, value = detail.partition("=")
                try:
                    attributes[key] = json.loads(value)
                except ValueError:
                    attributes[key] = value
        fact = DocumentFact(
            id=f"fatto-{len(facts) + 1:04d}",
            source_id=source.id,
            evidence_id=evidence.id,
            kind=kind,
            label=label,
            event_time=at,
            status=status,
            attributes=attributes,
        )
        facts.append(fact)
        dosages = attributes.get("dosageInstruction", attributes.get("dosage", []))
        if isinstance(dosages, dict):
            dosages = [dosages]
        if isinstance(dosages, list):
            for dosage in dosages:
                if not isinstance(dosage, dict):
                    continue
                quantities = [dosage.get("dose")]
                dose_and_rate = dosage.get("doseAndRate")
                if isinstance(dose_and_rate, list):
                    quantities.extend(
                        item.get("doseQuantity") for item in dose_and_rate if isinstance(item, dict)
                    )
                if any(
                    isinstance(q, dict)
                    and q.get("value") is not None
                    and not (q.get("unit") or q.get("code"))
                    for q in quantities
                ):
                    issues.append(
                        DocumentaryIssue(
                            code="dose_unit_missing",
                            message=(
                                "Quantità prescritta senza unità documentata: "
                                "non dedurre compresse o mg."
                            ),
                            fact_ids=(fact.id,),
                            evidence_ids=(evidence.id,),
                        )
                    )
                    break
        if kind == "Observation" and "value" in attributes:
            key = (label, at)
            previous = observations.get(key)
            if previous and any(
                previous.attributes.get(k) != attributes.get(k)
                for k in ("value", "unit", "comparator")
            ):
                issues.append(
                    DocumentaryIssue(
                        code="observation_disagreement",
                        message=(
                            "Risultati diversi con stessa etichetta e data: "
                            "verificare entrambe le fonti."
                        ),
                        fact_ids=(previous.id, fact.id),
                        evidence_ids=(previous.evidence_id, evidence.id),
                    )
                )
            observations[key] = fact
    return FactInventory(facts=tuple(facts), issues=tuple(issues))
