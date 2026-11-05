"""Identità locale e perimetro di accesso al campione MIMIC."""

import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LocalIdentity:
    subject: str
    token: str = field(repr=False)
    context_ids: frozenset[str]


def local_identities() -> tuple[LocalIdentity, ...]:
    """Abilita MIMIC solo con una credenziale privata e assegnazioni esplicite."""
    token = os.getenv("APP_MIMIC_TOKEN")
    assignments = os.getenv("APP_MIMIC_CONTEXTS")
    if token is None and assignments is None:
        return ()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,}", token or "") or not assignments:
        raise ValueError("Configurazione della credenziale MIMIC locale incompleta.")
    ids = frozenset(assignments.split(","))
    if not all(re.fullmatch(r"mimic-[0-9]+", identifier) for identifier in ids):
        raise ValueError("Assegnazioni MIMIC locali non valide.")
    return (LocalIdentity("mimic-locale", token, ids),)
