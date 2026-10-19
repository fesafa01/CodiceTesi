"""Servizio di importazione: coordina adapter e persistenza atomica."""

from pathlib import Path

from codice_tesi.backend.composition_repository import save_dataset
from codice_tesi.backend.database import Database
from codice_tesi.backend.datasets.ecream import load_ecream


async def import_ecream(database: Database, path: Path) -> tuple[bool, int]:
    """Coordina la conversione del formato esterno e la persistenza atomica. Tutte le regole
    specifiche delle note rimangono nell’adapter, non nel template."""
    _, loaded = load_ecream(path)
    return await save_dataset(
        database,
        list({context.id: context for context, _ in loaded}.values()),
        [source for _, source in loaded],
    )
