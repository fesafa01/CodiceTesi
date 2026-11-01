"""Schema e caricamento del solo template attivo, indipendente da dataset e SQL."""

import hashlib
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from pydantic import Field, model_validator

from codice_tesi.discharge.contracts import Contract

MISSING_VALUE = "Non disponibile nelle note fornite."


class Requirement(Contract):
    """Conserva insieme obbligatorietà e motivazione ricavata dai materiali Campus."""

    status: Literal["required", "optional", "unconfirmed", "conditional"]
    basis: str = Field(min_length=1)


class FieldDefinition(Contract):
    """Descrive un sottocampo; i vincoli di completezza sono scelte del prototipo.

    ``context_key`` identifica i soli valori copiabili dal contesto verificato.
    Gli altri contenuti devono provenire dalle fonti assegnate al campo.
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    type: Literal["text", "date", "boolean", "code"] = "text"
    required_for_completeness: bool = True
    instruction: str = Field(min_length=1)
    choices: tuple[str, ...] = ()
    context_key: Literal["patient_id", "episode_id"] | None = None


class Applicability(Contract):
    """Condizione dichiarativa riferita a un booleano di una sezione precedente."""

    section_id: str
    field_id: str
    equals: bool


class SectionDefinition(Contract):
    """Definisce contenuto e forma della sezione, senza conoscere i tipi delle fonti."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    requirement: Requirement
    repeatable: bool = False
    allows_documented_absence: bool = False
    instruction: str = Field(min_length=1)
    fields: tuple[FieldDefinition, ...] = Field(min_length=1)
    applies_when: Applicability | None = None

    @model_validator(mode="after")
    def unique_fields(self):
        """Evita che due sottocampi con lo stesso ID rendano ambiguo l'output."""
        if len({field.id for field in self.fields}) != len(self.fields):
            raise ValueError("Sottocampi duplicati nel template.")
        if self.allows_documented_absence and not self.repeatable:
            raise ValueError("L'assenza di elementi è prevista solo per sezioni ripetibili.")
        return self


class LetterTemplate(Contract):
    """Contratto versionato usato dal backend e dalle istruzioni della lettera."""

    schema_version: Literal["1.0"]
    id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    source_reference: str
    interpretation_notes: tuple[str, ...]
    sections: tuple[SectionDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_conditions(self):
        """Consente condizioni solo su booleani già definiti, evitando cicli."""
        previous = {}
        for section in self.sections:
            if section.id in previous:
                raise ValueError("Sezioni duplicate nel template.")
            if condition := section.applies_when:
                target = previous.get(condition.section_id)
                if target is None or target.repeatable:
                    raise ValueError(
                        "La condizione deve riferirsi a una sezione singola precedente."
                    )
                if not any(
                    f.id == condition.field_id and f.type == "boolean" for f in target.fields
                ):
                    raise ValueError("La condizione deve riferirsi a un campo booleano.")
            if (section.requirement.status == "conditional") != (section.applies_when is not None):
                raise ValueError("Obbligatorietà condizionale e condizione devono essere coerenti.")
            previous[section.id] = section
        return self

    @property
    def sha256(self) -> str:
        """Impronta del contenuto validato: rileva modifiche anche a versione invariata."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def section(self, section_id: str) -> SectionDefinition:
        """Restituisce la definizione canonica o rifiuta un ID estraneo al template."""
        for section in self.sections:
            if section.id == section_id:
                return section
        raise ValueError(f"Sezione non prevista dal template: {section_id}.")


@lru_cache(maxsize=1)
def load_template() -> LetterTemplate:
    """Carica la risorsa inclusa nel pacchetto; nessun fallback dipendente dal dataset."""
    resource = files("codice_tesi.discharge.templates").joinpath("campus_discharge.v1.json")
    return LetterTemplate.model_validate_json(resource.read_text(encoding="utf-8"))
