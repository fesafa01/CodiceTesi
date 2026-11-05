"""Autenticazione HTTP locale condivisa, indipendente dal database."""

from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from codice_tesi.access import LocalIdentity

bearer = HTTPBearer(
    auto_error=False,
    scheme_name="IdentitaLocale",
    description="Credenziale privata con assegnazioni ai ricoveri.",
)


def require_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> LocalIdentity:
    if credentials is not None:
        supplied = credentials.credentials.encode("utf-8")
        for identity in request.app.state.identities:
            if compare_digest(supplied, identity.token.encode("utf-8")):
                request.state.local_subject = identity.subject
                return identity
    raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
