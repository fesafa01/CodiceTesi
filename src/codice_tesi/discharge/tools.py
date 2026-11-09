"""Confine PydanticAI: elaborazioni locali, provenienza, budget e audit dei tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field
from pydantic_ai import ModelRetry, RunContext

from codice_tesi.discharge.documentary import DocumentaryAnalysis
from codice_tesi.discharge.facts import FactInventory
from codice_tesi.discharge.limits import EvidenceReadError, FinalPhaseToolCallError
from codice_tesi.discharge.provenance import EvidenceCatalog
from codice_tesi.observability.tool_spans import tool_span

if TYPE_CHECKING:
    from codice_tesi.observability.telemetry import Telemetry


@dataclass
class DocumentaryTools:
    catalog: EvidenceCatalog
    facts: FactInventory
    max_calls: int = 6
    max_result_bytes: int = 24000
    calls: list[dict] = field(default_factory=list)
    consulted: set[str] = field(default_factory=set)
    telemetry: Telemetry | None = None
    results: list[dict] = field(default_factory=list, repr=False)
    request_index: int | None = None
    final_only: bool = False

    def __post_init__(self):
        self._evidences = {e.id: e for e in self.catalog.evidences}
        self._sources = {s.id: s for s in self.catalog.sources}
        fact_ids = {fact.id for fact in self.facts.facts}
        if any(
            f.evidence_id not in self._evidences
            or self._evidences[f.evidence_id].source_id != f.source_id
            for f in self.facts.facts
        ):
            raise ValueError("Fatti fuori dal catalogo autorizzato.")
        if any(
            eid not in self._evidences for issue in self.facts.issues for eid in issue.evidence_ids
        ) or any(fid not in fact_ids for issue in self.facts.issues for fid in issue.fact_ids):
            raise ValueError("Rilievi fuori dal catalogo autorizzato.")
        self._analysis = DocumentaryAnalysis(self.facts)

    def execute(self, operation: str, **args) -> dict:
        if operation not in TOOL_NAMES:
            raise ValueError("Tool sconosciuto.")
        entry = {"tool": operation, "status": "started", "evidence_ids": []}
        with tool_span(
            self.telemetry, operation, len(self.calls) + 1, request_index=self.request_index
        ) as span:
            if len(self.calls) >= self.max_calls:
                if span is not None:
                    span.set_attribute("tesi.tool.failure_code", "call_limit")
                raise EvidenceReadError("call_limit")
            self.calls.append(entry)
            try:
                if self.final_only:
                    raise FinalPhaseToolCallError(operation)
                result = self._execute(operation, **args)
                ids = sorted(self._result_evidences(result["items"]))
                entry.update(
                    status="completed",
                    evidence_ids=ids,
                    results_count=len(result["items"]),
                    matches_total=result["matches_total"],
                )
                self.consulted.update(ids)
                self.results.append({"tool": operation, "result": result})
                if span is not None:
                    span.set_attributes(
                        {
                            "tesi.tool.results_count": len(result["items"]),
                            "tesi.tool.matches_total": result["matches_total"],
                            "tesi.tool.limited": result["limited"],
                            "tesi.tool.result_bytes": self._size(result),
                        }
                    )
                return result
            except Exception as exc:
                if span is not None and isinstance(
                    exc, (EvidenceReadError, FinalPhaseToolCallError)
                ):
                    span.set_attribute("tesi.tool.failure_code", exc.code)
                entry["status"] = "failed"
                entry["error_code"] = (
                    exc.code
                    if isinstance(exc, (EvidenceReadError, FinalPhaseToolCallError))
                    else "read_failed"
                )
                raise

    @staticmethod
    def _result_evidences(items):
        # Solo riferimenti del contratto: un JSON originale nei dosaggi potrebbe
        # avere chiavi omonime e non deve diventare provenienza dell'applicazione.
        result = set()
        for item in items:
            for event in item.get("events", [item]):
                result.add(event["evidence_id"])
                for issue in event.get("issues", []):
                    result.update(issue["evidence_ids"])
        return result

    @staticmethod
    def _size(value):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _execute(self, operation, **args):
        if operation == "leggi_evidenze":
            ids = args["ids"]
            if not ids or len(ids) > 10 or len(set(ids)) != len(ids):
                raise EvidenceReadError("invalid_ids")
            if any(eid not in self._evidences for eid in ids):
                raise EvidenceReadError("unknown_evidence")
            result = {
                "items": [],
                "source_metadata": {},
                "matches_total": len(ids),
                "limited": False,
            }
            for eid in ids:
                evidence = self._evidences[eid]
                source = self._sources[evidence.source_id]
                result["items"].append(
                    {
                        "evidence_id": eid,
                        "source_id": source.id,
                        "kind": source.kind,
                        "start": evidence.start,
                        "end": evidence.end,
                        "text": source.text[evidence.start : evidence.end],
                        "version": source.version,
                        "sha256": source.sha256,
                    }
                )
                result["source_metadata"][source.id] = {
                    "origin": source.origin,
                    "version": source.version,
                    "sha256": source.sha256,
                }
            if self._size(result) > self.max_result_bytes:
                raise EvidenceReadError("result_budget")
            return result
        limit, offset = args.pop("limit", 5), args.pop("offset", 0)
        if not 1 <= limit <= 10 or not 0 <= offset <= 10000:
            raise EvidenceReadError("invalid_limit")
        methods = {
            "riepiloga_osservazioni": self._analysis.observations,
            "ricostruisci_terapia_documentata": self._analysis.medications,
            "ricostruisci_sequenza_eventi": self._analysis.chronology,
        }
        items = methods[operation](**args)
        represented = {fact.evidence_id for fact in self.facts.facts}
        result = {
            "items": [],
            "matches_total": len(items),
            "limited": False,
            "offset": offset,
            "next_offset": None,
            "coverage": {
                "extraction_version": self.facts.extraction_version,
                "recognized_facts": len(self.facts.facts),
                "empty_reason": (
                    "inventory_empty"
                    if not self.facts.facts
                    else "no_matching_records"
                    if not items
                    else None
                ),
                "passages_not_interpreted": sum(
                    e.source_id in self._sources
                    and self._sources[e.source_id].kind != "contesto_verificato"
                    and e.id not in represented
                    for e in self.catalog.evidences
                ),
                "notice": "Copertura parziale dei record strutturati. Le note libere non sono "
                "interpretate. Risultati vuoti o limitati non dimostrano assenza clinica. "
                "Filtri per etichetta esatta, date inclusive come documentate, nessuna "
                "conversione di unità o equivalenza clinica.",
            },
        }
        for item in items[offset : offset + limit]:
            candidate = {
                **result,
                "items": [*result["items"], item],
                "limited": True,
                "next_offset": offset + len(result["items"]) + 1,
            }
            if self._size(candidate) > self.max_result_bytes:
                if not result["items"]:
                    raise EvidenceReadError("result_budget")
                break  # Pagine contigue: nessun gruppo saltato per farne entrare altri.
            result["items"].append(item)
        next_offset = offset + len(result["items"])
        result["limited"] = next_offset < len(items)
        result["next_offset"] = next_offset if result["limited"] else None
        if self._size(result) > self.max_result_bytes:
            raise EvidenceReadError("result_budget")
        return result

    def audit(self) -> dict:
        # I risultati sono destinati al modello, mai all'audit o alla telemetria.
        return {"calls": self.calls, "consulted_evidence_ids": sorted(self.consulted)}

    def summary(self) -> dict:
        """Contatori tecnici senza testi, etichette, argomenti o ID delle evidenze."""
        return {
            "extraction_version": self.facts.extraction_version,
            "recognized_facts": len(self.facts.facts),
            "executed": len(self.calls),
            "completed": sum(c["status"] == "completed" for c in self.calls),
            "failed": sum(c["status"] == "failed" for c in self.calls),
            "empty": sum(
                c["status"] == "completed" and c["matches_total"] == 0 for c in self.calls
            ),
            "nonempty": sum(
                c["status"] == "completed" and c["matches_total"] > 0 for c in self.calls
            ),
        }


def _execute_for_model(ctx: RunContext[DocumentaryTools], operation: str, **args) -> dict:
    try:
        return ctx.deps.execute(operation, **args)
    except EvidenceReadError as exc:
        raise ModelRetry(str(exc)) from exc


Limit = Annotated[int, Field(ge=1, le=10)]
Offset = Annotated[int, Field(ge=0, le=10000)]
Label = Annotated[str, Field(min_length=1, max_length=200)]


async def riepiloga_osservazioni(
    ctx: RunContext[DocumentaryTools],
    esame: Label | None = None,
    dal: str | None = None,
    al: str | None = None,
    storico: bool = False,
    limit: Limit = 5,
    offset: Offset = 0,
) -> dict:
    """Calcola gli ultimi record Observation per etichetta, inclusi esami e parametri vitali.

    Esame è l'etichetta esatta; storico include tutte le misurazioni. Mantiene parità,
    stati, unità e fonti senza conversioni né giudizi clinici. Date AAAA-MM-GG inclusive.
    Limit conta gruppi; next_offset permette di leggere la pagina successiva.
    """
    return _execute_for_model(
        ctx,
        "riepiloga_osservazioni",
        esame=esame,
        dal=dal,
        al=al,
        storico=storico,
        limit=limit,
        offset=offset,
    )


async def ricostruisci_terapia_documentata(
    ctx: RunContext[DocumentaryTools],
    farmaco: Label | None = None,
    dal: str | None = None,
    al: str | None = None,
    limit: Limit = 5,
    offset: Offset = 0,
) -> dict:
    """Raggruppa per etichetta esatta e ordina prescrizioni e somministrazioni distinte.

    Conserva dosaggi originali e lacune. Non deduce sospensioni, equivalenze o terapia
    domiciliare dall'ultimo evento. Date AAAA-MM-GG inclusive; limit conta gruppi.
    """
    return _execute_for_model(
        ctx,
        "ricostruisci_terapia_documentata",
        farmaco=farmaco,
        dal=dal,
        al=al,
        limit=limit,
        offset=offset,
    )


async def ricostruisci_sequenza_eventi(
    ctx: RunContext[DocumentaryTools],
    tipo: Literal["Observation", "MedicationRequest", "MedicationAdministration", "Procedure"]
    | None = None,
    dal: str | None = None,
    al: str | None = None,
    limit: Limit = 5,
    offset: Offset = 0,
) -> dict:
    """Ordina i fatti strutturati documentati, con stato, attributi e riferimenti.

    Non interpreta note libere o cause cliniche. Date AAAA-MM-GG inclusive.
    Distingue istanti con fuso, orari locali documentati e sola data ambigua.
    """
    return _execute_for_model(
        ctx, "ricostruisci_sequenza_eventi", tipo=tipo, dal=dal, al=al, limit=limit, offset=offset
    )


async def leggi_evidenze(
    ctx: RunContext[DocumentaryTools], ids: Annotated[list[str], Field(min_length=1, max_length=10)]
) -> dict:
    """Ispeziona passaggi originali tramite ID, per verificare un risultato elaborato."""
    return _execute_for_model(ctx, "leggi_evidenze", ids=ids)


DOCUMENTARY_TOOLS = [
    riepiloga_osservazioni,
    ricostruisci_terapia_documentata,
    ricostruisci_sequenza_eventi,
    leggi_evidenze,
]
TOOL_NAMES = frozenset(tool.__name__ for tool in DOCUMENTARY_TOOLS)
