"""Autenticazione HTTP dimostrativa condivisa, indipendente dal database."""

from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from codice_tesi.demo_access import DemoIdentity

bearer = HTTPBearer(
    auto_error=False,
    scheme_name="IdentitaLocaleDemo",
    description="Token fittizi locali: demo-medico-a, demo-medico-b, demo-revisore.",
)


def require_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> DemoIdentity:
    if credentials is not None:
        supplied = credentials.credentials.encode("utf-8")
        for identity in request.app.state.demo_identities:
            if compare_digest(supplied, identity.token.encode("utf-8")):
                request.state.demo_subject = identity.subject
                return identity
    raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
