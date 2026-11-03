"""Autorizzazione ai contesti del backend documentale."""

from typing import Annotated

from fastapi import Depends

from codice_tesi.backend.repository import BackendError
from codice_tesi.demo_access import DemoIdentity
from codice_tesi.http_auth import require_identity


def require_context_identity(
    context_id: str, identity: Annotated[DemoIdentity, Depends(require_identity)]
) -> DemoIdentity:
    if context_id not in identity.context_ids:
        raise BackendError(403, "ACCESSO_NEGATO", "Contesto non accessibile per questa identità.")
    return identity
