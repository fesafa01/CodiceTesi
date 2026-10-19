"""Contratto indipendente dal dataset e piano deterministico della dimissione."""

import hashlib
from typing import Literal

from pydantic import Field, model_validator

from codice_tesi.discharge.contracts import Contract
from codice_tesi.discharge.template import LetterTemplate


class Context(Contract):
    """Identità verificata dell’episodio oppure nota isolata priva di legami clinici."""

    id: str
    dataset: str
    dataset_version: str
    scope: Literal["nota_isolata", "gruppo_note", "episodio"]
    patient_id: str | None = None
    episode_id: str | None = None
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def identity(self):
        """Impedisce di usare una nota anonimizzata come identità di paziente o episodio."""
        if self.scope == "episodio" and (not self.patient_id or not self.episode_id):
            raise ValueError(
                "Un episodio richiede identificativi verificati di paziente ed episodio."
            )
        if self.scope != "episodio" and (self.patient_id or self.episode_id):
            raise ValueError("Un contesto di note non identifica un paziente o un episodio.")
        return self


class EvidenceSource(Contract):
    """Testo originale con identità, versione, origine e impronta verificata."""

    id: str
    context_id: str
    kind: str
    text: str = Field(min_length=1)
    origin: str
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def integrity(self):
        """Controlla che il testo ricevuto corrisponda all’impronta dichiarata."""
        if hashlib.sha256(self.text.encode()).hexdigest() != self.sha256:
            raise ValueError("Hash della fonte non coerente.")
        return self


class SectionInput(Contract):
    """Associazione calcolata dal codice tra una sezione del template e le sue fonti."""

    section_id: str
    sources: tuple[EvidenceSource, ...]


class CompositionPacket(Contract):
    """Snapshot: stesso contesto di note per tutti i campi, senza filtro per tipologia."""

    schema_version: Literal["2.0"] = "2.0"
    policy_version: str = "campus-context-v2"
    template: LetterTemplate
    context: Context
    sections: tuple[SectionInput, ...]
    excluded_source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def isolation(self):
        """Verifica ordine, identità e versioni prima di aprire una sessione di stesura."""
        if tuple(s.section_id for s in self.sections) != tuple(
            s.id for s in self.template.sections
        ):
            raise ValueError("Piano delle sezioni diverso dal template.")
        seen = {}
        for section in self.sections:
            ids = [source.id for source in section.sources]
            if ids != sorted(set(ids)):
                raise ValueError("Le fonti devono essere univoche e ordinate.")
            for source in section.sources:
                if source.context_id != self.context.id:
                    raise ValueError("Fonte fuori contesto.")
                if source.id in seen and seen[source.id] != source:
                    raise ValueError("Versioni discordanti della stessa fonte.")
                seen[source.id] = source
        return self
