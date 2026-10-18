"""Contratti pubblici delle API cliniche, senza percorsi del filesystem o annotazioni attese."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Metadata(Contract):
    scenario: str | None
    esecuzione_id: str | None
    revisione: int
    istante: AwareDatetime
    versione_dataset: str | None
    request_id: str
    dataset_caricato: bool
    canali_non_disponibili: list[str] = Field(default_factory=list)


class Pagination(Contract):
    prossimo_cursore: str | None = None
    completa: bool


class Response[T](Contract):
    meta: Metadata
    dati: T
    paginazione: Pagination | None = None


class Problem(Contract):
    type: str
    title: str
    status: int
    detail: str
    request_id: str


class UnitRead(Contract):
    id: str
    nome: str
    tipo: str


class PatientRead(Contract):
    id: str
    nome: str
    cognome: str
    data_nascita: date


class EpisodeRead(Contract):
    id: str
    paziente_id: str
    tipo: str
    stato: str
    ingresso_il: AwareDatetime
    nosologico: str | None = None
    unita_clinica_id: str | None = None
    unita_degenza_id: str | None = None
    letto: str | None = None
    dimissione_prevista_il: date | None = None
    dimissione_effettiva_il: AwareDatetime | None = None


class AdmissionContext(Contract):
    ricovero: EpisodeRead
    paziente: PatientRead


class SourceRead(Contract):
    id: str
    paziente_id: str
    episodio_id: str
    categoria: Literal["registrazione", "documento"]
    tipo: str
    titolo: str
    sistema_origine: str
    autore_id: str | None
    canali: list[str]
    data_clinica: AwareDatetime
    registrato_il: AwareDatetime
    disponibile_il: AwareDatetime
    versione: int
    stato: str
    stato_corrente: str
    sintetico: Literal[True] = True


class SectionRead(Contract):
    id: str
    titolo: str
    testo: str
    pagina: int | None = None


class SourceDetail(SourceRead):
    testo: str
    sezioni: list[SectionRead]
    contenuto_url: str | None = None
    tipo_contenuto: Literal["application/pdf"] | None = None
    sha256: str | None = None
    stato_estrazione: Literal["disponibile", "non_disponibile", "non_applicabile"]
    metodo_estrazione: str | None = None


class ResultRead(Contract):
    id: str
    tipo: str
    esame: str
    valore: Decimal
    unita: str
    codice_unita: str | None = None
    codice: str | None = None
    sistema_codifica: str | None = None
    data_clinica: AwareDatetime
    fonte: SourceRead


class CoverageRead(Contract):
    modulo: str
    stato: str


class TemplateField(Contract):
    id: str
    etichetta: str
    obbligatorio: bool | None
    attendibilita_obbligatorieta: str


class TemplateRead(Contract):
    id: str
    versione: int
    unita_clinica_id: str
    campi: list[TemplateField]


class ChangeRead(Contract):
    fonte_id: str
    modifica: Literal["aggiunta", "corretta", "ritirata"]
    versione_precedente: int | None
    versione_corrente: int | None
