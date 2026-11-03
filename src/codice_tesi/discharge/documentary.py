"""Elaborazioni deterministiche dei fatti, senza dipendenze da agenti o telemetria.

Le etichette sono confrontate esattamente: nessuna equivalenza farmacologica,
conversione di unità o interpretazione clinica viene dedotta dal testo.
"""

from collections import defaultdict
from datetime import UTC, date, datetime

from codice_tesi.discharge.facts import DocumentFact, FactInventory
from codice_tesi.discharge.limits import EvidenceReadError

EVENT_TYPES = {"Observation", "MedicationRequest", "MedicationAdministration", "Procedure"}


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed


def _ordered(facts: list[DocumentFact]) -> tuple[list[DocumentFact], bool]:
    # Date prive di ora/fuso non consentono un ordinamento assoluto con timestamp
    # completi. In quel caso si espone soltanto l'ordine delle date documentate.
    precise = all(
        fact.event_time and "T" in fact.event_time and _instant(fact.event_time).tzinfo
        for fact in facts
    )
    return sorted(
        facts,
        key=lambda f: (_instant(f.event_time) if precise else f.event_time[:10], f.id),
    ), bool(precise)


def _missing_dosage_fields(dosage) -> list[str]:
    instructions = dosage if isinstance(dosage, list) else [dosage]
    instructions = [item for item in instructions if isinstance(item, dict)]
    missing = set() if instructions else {"dose", "route", "regimen_timing"}
    for item in instructions:
        present = set()
        rates = item.get("doseAndRate", [])
        rates = rates if isinstance(rates, list) else []
        if item.get("dose") or any(
            part.get("doseQuantity") or part.get("doseRange")
            for part in rates
            if isinstance(part, dict)
        ):
            present.add("dose")
        if item.get("route"):
            present.add("route")
        if item.get("timing"):
            present.add("regimen_timing")
        missing.update({"dose", "route", "regimen_timing"} - present)
    return sorted(missing)


class DocumentaryAnalysis:
    def __init__(self, inventory: FactInventory):
        self.inventory = inventory

    def _select(self, kinds, dal=None, al=None, label=None):
        for value in (dal, al):
            if value:
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise EvidenceReadError("invalid_date") from None
        if dal and al and dal > al:
            raise EvidenceReadError("inverted_dates")
        return [
            fact
            for fact in self.inventory.facts
            if fact.kind in kinds
            and fact.event_time
            and (not dal or fact.event_time[:10] >= dal)
            and (not al or fact.event_time[:10] <= al)
            and (label is None or fact.label == label)
        ]

    def _event(self, fact):
        return {
            **fact.model_dump(mode="json"),
            "issues": [
                issue.model_dump(mode="json")
                for issue in self.inventory.issues
                if fact.id in issue.fact_ids
            ],
        }

    def observations(self, *, esame=None, dal=None, al=None, storico=False):
        groups = defaultdict(list)
        for fact in self._select({"Observation"}, dal, al, esame):
            groups[fact.label].append(fact)
        items = []
        for label, facts in sorted(groups.items()):
            ordered, precise = _ordered(facts)
            last = ordered[-1]
            latest = [
                fact
                for fact in ordered
                if (
                    _instant(fact.event_time) == _instant(last.event_time)
                    if precise
                    else fact.event_time[:10] == last.event_time[:10]
                )
            ]
            items.append(
                {
                    "label": label,
                    "documented_count": len(facts),
                    "ordering": "instant" if precise else "documented_date_only",
                    "latest_ambiguous": len(latest) > 1,
                    "events": [self._event(f) for f in ordered if storico or f in latest],
                    "notice": (
                        "Observation, non necessariamente laboratorio. Ultimo record documentato, "
                        "senza selezione per stato, conversioni di unità o giudizi di normalità. "
                        "Stessa etichetta non garantisce stesso campione o metodo."
                    ),
                }
            )
        return items

    def medications(self, *, farmaco=None, dal=None, al=None):
        groups = defaultdict(list)
        for fact in self._select(
            {"MedicationRequest", "MedicationAdministration"}, dal, al, farmaco
        ):
            groups[fact.label].append(fact)
        items = []
        for label, facts in sorted(groups.items()):
            ordered, precise = _ordered(facts)
            events = []
            for fact in ordered:
                dosage = fact.attributes.get("dosageInstruction", fact.attributes.get("dosage", []))
                # Mantiene il JSON originale: presenza del campo non attesta la
                # completezza della prescrizione e non completa unità mancanti.
                events.append(
                    {
                        **self._event(fact),
                        "record_role": "prescription"
                        if fact.kind == "MedicationRequest"
                        else "administration",
                        "dosage_documented": bool(dosage),
                        "missing_structured_fields": _missing_dosage_fields(dosage),
                    }
                )
            items.append(
                {
                    "label": label,
                    "ordering": "instant" if precise else "documented_date_only",
                    "events": events,
                    "notice": (
                        "Sequenza documentale, non riconciliazione clinica. Prescrizioni e "
                        "somministrazioni restano distinte. Stato active o ultimo evento non "
                        "dimostrano terapia domiciliare; etichette diverse non vengono unite."
                    ),
                }
            )
        return items

    def chronology(self, *, tipo=None, dal=None, al=None):
        if tipo is not None and tipo not in EVENT_TYPES:
            raise EvidenceReadError("invalid_type")
        ordered, precise = _ordered(self._select({tipo} if tipo else EVENT_TYPES, dal, al))
        return [
            {**self._event(fact), "ordering": "instant" if precise else "documented_date_only"}
            for fact in ordered
        ]
