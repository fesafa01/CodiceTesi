"""Rappresentazione di importazione condivisa; distinta dai contratti HTTP."""

from dataclasses import dataclass, field
from datetime import UTC, datetime


def aware(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Il dataset deve specificare il fuso orario.")
    return result.astimezone(UTC)


@dataclass
class ImportedSource:
    identity: dict
    title: str
    clinical_at: datetime
    recorded_at: datetime
    available_at: datetime
    status: str
    sections: list[dict]
    version: int = 1
    measurements: list[dict] = field(default_factory=list)
    pdf: bytes | None = None
    sha256: str | None = None
    extraction_status: str = "non_applicabile"
    extraction_method: str | None = None
