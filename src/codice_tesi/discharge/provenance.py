"""Catalogo locale e citazioni per sezione, senza verifica semantica automatica."""

import json
import re
from hashlib import sha256
from typing import Literal

from pydantic import Field, model_validator

from codice_tesi.discharge.contracts import Contract
from codice_tesi.discharge.facts import FactInventory
from codice_tesi.discharge.models import CompositionPacket, EvidenceSource
from codice_tesi.discharge.segmentation import SEGMENTATION_VERSION, source_intervals
from codice_tesi.discharge.template import MISSING_VALUE, LetterTemplate, SectionDefinition

TRACE_SCHEMA_VERSION = "1.1"
DRAFT_TITLE = "BOZZA DI LETTERA DI DIMISSIONE - DA REVISIONARE"


class Evidence(Contract):
    """Intervallo di punti di codice Unicode nel testo originale fornito al modello."""

    id: str = Field(min_length=1, pattern=r"\S")
    source_id: str = Field(min_length=1, pattern=r"\S")
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)


class EvidenceCatalog(Contract):
    segmentation_version: str = "fonte-completa-v1"
    sources: tuple[EvidenceSource, ...] = Field(min_length=1)
    evidences: tuple[Evidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_catalog(self):
        sources = {source.id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("Fonti duplicate nel catalogo.")
        if len({source.context_id for source in self.sources}) != 1:
            raise ValueError("Fonte fuori contesto nel catalogo.")
        if any(not source.id.strip() or not source.text.strip() for source in self.sources):
            raise ValueError("ID o testo della fonte vuoto nel catalogo.")
        if len({evidence.id for evidence in self.evidences}) != len(self.evidences):
            raise ValueError("Evidenze duplicate nel catalogo.")
        for evidence in self.evidences:
            source = sources.get(evidence.source_id)
            if source is None:
                raise ValueError("Evidenza associata a una fonte sconosciuta.")
            if not 0 <= evidence.start < evidence.end <= len(source.text):
                raise ValueError("Offset dell'evidenza fuori dal testo della fonte.")
            if not source.text[evidence.start : evidence.end].strip():
                raise ValueError("Estratto dell'evidenza vuoto.")
        return self


def build_catalog(packet: CompositionPacket) -> EvidenceCatalog:
    """Passaggi originali deterministici, incluso il contesto verificato."""
    source_ids = {source.id for source in packet.sources}
    index = 1
    while (context_source_id := f"contesto-verificato-{index:04d}") in source_ids:
        index += 1
    context = packet.context
    context_text = "\n".join(
        [
            f"Ambito: {context.scope}",
            f"Identificativo paziente: {context.patient_id or 'non disponibile'}",
            f"Identificativo episodio: {context.episode_id or 'non disponibile'}",
            *context.limitations,
        ]
    )
    technical_source = EvidenceSource(
        id=context_source_id,
        context_id=context.id,
        kind="contesto_verificato",
        text=context_text,
        origin="CompositionPacket.context",
        version=f"{packet.schema_version}/{context.dataset_version}",
        sha256=sha256(context_text.encode("utf-8")).hexdigest(),
    )
    sources = (*packet.sources, technical_source)
    intervals = [
        (source, start, end) for source in sources for start, end in source_intervals(source)
    ]
    return EvidenceCatalog(
        segmentation_version=SEGMENTATION_VERSION,
        sources=sources,
        evidences=tuple(
            Evidence(id=f"evidenza-{i:04d}", source_id=source.id, start=start, end=end)
            for i, (source, start, end) in enumerate(intervals, 1)
        ),
    )


class Citation(Contract):
    source_id: str = Field(min_length=1, pattern=r"\S")
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_evidences(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Evidenze duplicate nella citazione.")
        return self


class DraftSection(Contract):
    section_id: str = Field(min_length=1)
    text: str
    sources: tuple[Citation, ...]

    @model_validator(mode="after")
    def unique_sources(self):
        if len({citation.source_id for citation in self.sources}) != len(self.sources):
            raise ValueError("Fonti duplicate nella sezione.")
        return self


class StructuredDraft(Contract):
    sections: tuple[DraftSection, ...] = Field(min_length=1)


class DatasetBinding(Contract):
    id: str
    version: str


class TemplateBinding(Contract):
    id: str
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TraceBinding(Contract):
    run_id: str = Field(min_length=1)
    context_id: str
    dataset: DatasetBinding
    template: TemplateBinding


class TraceValidation(Contract):
    technical: Literal["not_run", "passed", "failed"]
    clinical_review: Literal["not_performed"] = "not_performed"


class TraceResult(Contract):
    schema_version: Literal["1.0", "1.1"] = TRACE_SCHEMA_VERSION
    binding: TraceBinding
    catalog: EvidenceCatalog
    draft: StructuredDraft
    validation: TraceValidation
    facts: FactInventory = Field(default_factory=FactInventory)

    @model_validator(mode="after")
    def matching_context(self):
        if any(source.context_id != self.binding.context_id for source in self.catalog.sources):
            raise ValueError("Fonte fuori dal contesto del risultato.")
        evidences = {e.id: e for e in self.catalog.evidences}
        fact_ids = {fact.id for fact in self.facts.facts}
        if len(fact_ids) != len(self.facts.facts):
            raise ValueError("Fatti documentali duplicati.")
        for fact in self.facts.facts:
            evidence = evidences.get(fact.evidence_id)
            if evidence is None or evidence.source_id != fact.source_id:
                raise ValueError("Fatto documentale fuori dal catalogo.")
        for issue in self.facts.issues:
            if (
                not set(issue.fact_ids) <= fact_ids
                or not set(issue.evidence_ids) <= evidences.keys()
            ):
                raise ValueError("Rilievo documentale con riferimenti sconosciuti.")
        return self


def _unique_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Chiave JSON duplicata nella risposta.")
        result[key] = value
    return result


def draft_json_text(raw: str, *, partial: bool = False) -> str:
    """Accetta solo JSON grezzo o un unico involucro Markdown JSON, senza prosa."""
    text = raw.strip()
    if text.startswith("```"):
        header, opening, body = text.partition("\n")
        if opening and header.strip().casefold() in {"```", "```json"}:
            content, closing, trailer = body.rpartition("\n")
            if closing and trailer.strip() == "```":
                return content
            if partial:
                return body
    return text


def _only_missing_fields(text: str, definition: SectionDefinition) -> bool:
    """Riconosce solo tutti i campi canonici esplicitamente marcati indisponibili."""
    labels = {field.label.casefold() for field in definition.fields}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(labels) != len(definition.fields) or len(lines) != len(labels):
        return False
    missing = {"non disponibile", "non disponibile.", MISSING_VALUE.casefold()}
    seen = set()
    for line in lines:
        label, colon, value = line.partition(":")
        label = label.strip().casefold()
        if (
            not colon
            or label not in labels
            or label in seen
            or value.strip().casefold() not in missing
        ):
            return False
        seen.add(label)
    return seen == labels


def parse_draft(raw: str, catalog: EvidenceCatalog, template: LetterTemplate) -> StructuredDraft:
    """Convalida sezioni e riferimenti senza modificare il contenuto del modello."""
    draft = StructuredDraft.model_validate(
        json.loads(draft_json_text(raw), object_pairs_hook=_unique_json_keys)
    )
    if [section.section_id for section in draft.sections] != [s.id for s in template.sections]:
        raise ValueError("Le sezioni JSON devono essere tutte presenti una volta in ordine.")
    sources = {source.id: source for source in catalog.sources}
    evidences = {evidence.id: evidence for evidence in catalog.evidences}
    for section, definition in zip(draft.sections, template.sections, strict=True):
        text = section.text.strip()
        if not text and definition.requirement.status in {"required", "unconfirmed"}:
            raise ValueError("Sezione obbligatoria vuota: usare la formula del dato mancante.")
        if not text or text == MISSING_VALUE or _only_missing_fields(text, definition):
            if section.sources:
                raise ValueError("Una sezione vuota o interamente indisponibile non cita fonti.")
        elif not section.sources:
            raise ValueError("Sezione con contenuto senza citazioni.")
        for citation in section.sources:
            if citation.source_id not in sources:
                raise ValueError("Citazione di una fonte sconosciuta.")
            for evidence_id in citation.evidence_ids:
                evidence = evidences.get(evidence_id)
                if evidence is None:
                    raise ValueError("Citazione di un'evidenza sconosciuta.")
                if evidence.source_id != citation.source_id:
                    raise ValueError("L'evidenza citata appartiene a un'altra fonte.")
    return draft


def render_draft(draft: StructuredDraft, template: LetterTemplate) -> str:
    """Intestazioni applicative canoniche, testo clinico separato dai riferimenti."""
    return (
        DRAFT_TITLE
        + "\n"
        + "\n".join(
            f"{index}. {definition.title}\n{section.text}\n"
            for index, (definition, section) in enumerate(
                zip(template.sections, draft.sections, strict=True), 1
            )
        )
    )


def _literal_block(text: str) -> str:
    fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", text)), default=0))
    return f"{fence}text\n{text}\n{fence}"


def render_evidences(result: TraceResult, template: LetterTemplate) -> str:
    """Mostra testo e provenienza dal catalogo originale, mai da estratti inventati dal LLM."""
    sources = {source.id: source for source in result.catalog.sources}
    evidences = {evidence.id: evidence for evidence in result.catalog.evidences}
    lines = [
        "# Fonti della bozza — da revisionare",
        "",
        "Citazioni proposte dal modello per sezione. "
        + {
            "passed": "Controlli tecnici superati; ",
            "not_run": "Controlli tecnici non eseguiti; ",
            "failed": "Controlli tecnici falliti; ",
        }[result.validation.technical]
        + "pertinenza, completezza e correttezza clinica non verificate.",
        "Localizzazione: " + result.catalog.segmentation_version + ". "
        "I riferimenti riguardano questa versione della bozza.",
        "",
    ]
    for index, (definition, section) in enumerate(
        zip(template.sections, result.draft.sections, strict=True), 1
    ):
        lines.extend([f"## {index}. {definition.title}", "", _literal_block(section.text), ""])
        if not section.sources:
            lines.extend(["Nessuna citazione proposta per questa sezione.", ""])
        for citation in section.sources:
            source = sources[citation.source_id]
            metadata = (
                f"Fonte: {source.id}\nOrigine: {source.origin}\nVersione: {source.version}\n"
                f"SHA-256: {source.sha256}"
            )
            lines.extend([_literal_block(metadata), ""])
            for evidence_id in citation.evidence_ids:
                evidence = evidences[evidence_id]
                lines.extend(
                    [
                        _literal_block(
                            f"Evidenza: {evidence.id} — "
                            f"caratteri [{evidence.start}, {evidence.end})"
                        ),
                        "",
                        _literal_block(source.text[evidence.start : evidence.end]),
                        "",
                    ]
                )
    return "\n".join(lines)
