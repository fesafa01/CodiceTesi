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


def _ordered(facts: list[DocumentFact]) -> tuple[list[DocumentFact], str]:
    # Orari tutti locali si confrontano come documentati, senza inventare fusi.
    # Date senza ora o una miscela di orari con/senza fuso impongono la sola data.
    clocks = bool(facts) and all(f.event_time and "T" in f.event_time for f in facts)
    zones = {_instant(f.event_time).tzinfo is not None for f in facts} if clocks else set()
    ordering = (
        "instant"
        if zones == {True}
        else "documented_local_time"
        if zones == {False}
        else "documented_date_only"
    )
    return sorted(
        facts,
        key=lambda f: (
            _instant(f.event_time) if ordering != "documented_date_only" else f.event_time[:10],
            f.id,
        ),
    ), ordering


def _medication_details(fact):
    role = fact.attributes.get("record_role")
    if role:
        fields = fact.attributes["record_fields"]
        dose = (
            ("dose_given", "amount")
            if fact.kind == "MedicationAdministration"
            else ("dose_val_rx",)
        )
        units = (
            ("dose_given_unit", "amountuom")
            if fact.kind == "MedicationAdministration"
            else ("dose_unit_rx",)
        )
        timing = ("frequency", "doses_per_24_hrs", "disp_sched", "infusion_rate", "rate")
        groups = {"dose": dose, "dose_unit": units, "route": ("route",), "regimen_timing": timing}

        # Le liste di attributi discordanti restano nel risultato; non attestano
        # una dose univoca. La data di un evento non è un regime di somministrazione.
        def present(names):
            return any(isinstance(fields.get(k), str) and fields[k] for k in names)

        return {
            "record_role": role,
            "dosage_documented": present((*dose, *units, "route", *timing)),
            "missing_structured_fields": [
                key for key, names in groups.items() if not present(names)
            ],
        }
    dosage = fact.attributes.get("dosageInstruction", fact.attributes.get("dosage", []))
    return {
        "record_role": "prescription" if fact.kind == "MedicationRequest" else "administration",
        "dosage_documented": bool(dosage),
        "missing_structured_fields": _missing_dosage_fields(dosage),
    }


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
            ordered, ordering = _ordered(facts)
            last = ordered[-1]
            latest = [
                fact
                for fact in ordered
                if (
                    _instant(fact.event_time) == _instant(last.event_time)
                    if ordering != "documented_date_only"
                    else fact.event_time[:10] == last.event_time[:10]
                )
            ]
            items.append(
                {
                    "label": label,
                    "documented_count": len(facts),
                    "ordering": ordering,
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
            ordered, ordering = _ordered(facts)
            events = []
            for fact in ordered:
                # Mantiene il JSON originale: presenza del campo non attesta la
                # completezza della prescrizione e non completa unità mancanti.
                events.append(
                    {
                        **self._event(fact),
                        **_medication_details(fact),
                    }
                )
            items.append(
                {
                    "label": label,
                    "ordering": ordering,
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
        ordered, ordering = _ordered(self._select({tipo} if tipo else EVENT_TYPES, dal, al))
        return [{**self._event(fact), "ordering": ordering} for fact in ordered]
