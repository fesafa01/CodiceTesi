"""Contesto, note e template della singola richiesta di dimissione."""

import hashlib
from typing import Literal

from pydantic import Field, model_validator

from codice_tesi.discharge.contracts import Contract
from codice_tesi.discharge.template import LetterTemplate, load_template


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


class CompositionPacket(Contract):
    """Un solo contesto con tutte le note e il template Campus canonico."""

    schema_version: Literal["3.0"] = "3.0"
    template: LetterTemplate
    context: Context
    sources: tuple[EvidenceSource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def isolation(self):
        """Valida una volta template, ordine e appartenenza delle note."""
        if self.template != load_template():
            raise ValueError("Template non aggiornato: ricostruire anche il backend.")
        ids = [source.id for source in self.sources]
        if ids != sorted(set(ids)):
            raise ValueError("Le fonti devono essere univoche e ordinate.")
        if any(source.context_id != self.context.id for source in self.sources):
            raise ValueError("Fonte fuori contesto.")
        return self
