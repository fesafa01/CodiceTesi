"""Righe PostgreSQL tipizzate: lo schema e i vincoli sono in schema.sql, senza ORM."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Row(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Unit(Row):
    id: str
    name: str
    kind: str


class Patient(Row):
    id: str
    given_name: str
    family_name: str
    birth_date: date


class Episode(Row):
    id: str
    patient_id: str
    kind: str
    status: str
    started_at: datetime
    nosological_id: str | None = None
    unit_id: str | None = None
    ward_id: str | None = None
    bed: str | None = None
    planned_discharge: date | None = None
    discharged_at: datetime | None = None
    description: str | None = None


class Source(Row):
    id: str
    patient_id: str
    episode_id: str
    category: str
    kind: str
    origin: str
    author_id: str | None = None


class SourceVersion(Row):
    scenario_id: str
    source_id: str
    version: int
    title: str
    clinical_at: datetime
    recorded_at: datetime
    available_at: datetime
    status: str
    text: str = ""
    sections: list[dict] = Field(default_factory=list)
    pdf: bytes | None = None
    sha256: str | None = None
    pdf_size: int | None = None
    extraction_status: str
    extraction_method: str | None = None


class Measurement(Row):
    scenario_id: str
    source_id: str
    version: int
    ordinal: int
    kind: str
    name: str
    value: Decimal
    unit: str
    clinical_at: datetime
    unit_code: str | None = None
    code: str | None = None
    code_system: str | None = None
