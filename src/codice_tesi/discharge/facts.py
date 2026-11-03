"""Fatti documentali estratti dai record testuali strutturati, senza inferenza clinica.

Legge la rappresentazione già preparata dall'adattatore Synthea, anche nei
pacchetti esistenti. Le note libere non vengono interpretate automaticamente.
"""

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import JsonValue

from codice_tesi.discharge.contracts import Contract

if TYPE_CHECKING:
    from codice_tesi.discharge.provenance import EvidenceCatalog


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


def extract_facts(catalog: "EvidenceCatalog") -> FactInventory:
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
