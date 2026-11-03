"""API di lettura: dati clinici → database → pacchetto della lettera."""

import json
import logging
import re
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException

from codice_tesi.backend.auth import require_context_identity
from codice_tesi.backend.database import BackendSettings, Database
from codice_tesi.backend.repository import BackendError, list_contexts, read_packet
from codice_tesi.demo_access import DEMO_IDENTITIES, DemoIdentity
from codice_tesi.discharge.models import CompositionPacket, Context
from codice_tesi.discharge.template import LetterTemplate, load_template
from codice_tesi.http_auth import require_identity

logger = logging.getLogger("clinical_backend")
router = APIRouter(
    prefix="/api/v1/compilazione",
    tags=["Compilazione"],
    dependencies=[Depends(require_identity)],
)


@router.get("/template", response_model=LetterTemplate)
async def active_template():
    """Espone il template Campus incluso nell'applicazione."""
    return load_template()


@router.get("/contesti", response_model=list[Context])
async def contexts(
    request: Request,
    identity: Annotated[DemoIdentity, Depends(require_identity)],
):
    return await list_contexts(request.app.state.database, allowed_context_ids=identity.context_ids)


@router.get("/avvio-test", response_model=CompositionPacket)
async def bootstrap(
    request: Request,
    seed: Annotated[int, Query(ge=0)],
    identity: Annotated[DemoIdentity, Depends(require_identity)],
):
    """Seleziona soltanto fra i contesti assegnati all'identità fittizia."""
    return await read_packet(
        request.app.state.database, seed=seed, allowed_context_ids=identity.context_ids
    )


@router.get("/contesti/{context_id}/pacchetto", response_model=CompositionPacket)
async def packet(
    request: Request,
    context_id: str,
    identity: Annotated[DemoIdentity, Depends(require_context_identity)],
):
    """Legge il contesto richiesto, le sue fonti e il template Campus."""
    return await read_packet(
        request.app.state.database,
        context_id=context_id,
        allowed_context_ids=identity.context_ids,
    )


def problem(request, status, code, detail):
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"urn:clinical-backend:error:{code.lower()}",
            "title": code,
            "status": status,
            "detail": detail,
            "request_id": request.state.request_id,
        },
    )


def create_app(
    settings: BackendSettings | None = None,
    *,
    demo_identities: tuple[DemoIdentity, ...] = DEMO_IDENTITIES,
) -> FastAPI:
    settings = settings or BackendSettings()
    if (
        not demo_identities
        or any(not identity.subject or not identity.token for identity in demo_identities)
        or len({identity.token for identity in demo_identities}) != len(demo_identities)
        or len({identity.subject for identity in demo_identities}) != len(demo_identities)
    ):
        raise ValueError("Le identità demo richiedono soggetti e token non vuoti e univoci.")

    @asynccontextmanager
    async def lifespan(app):
        async with Database(settings) as database:
            app.state.database = database
            yield

    app = FastAPI(
        title="Backend documentale clinico",
        version="0.4.0",
        lifespan=lifespan,
        description="Contesti e fonti per la compilazione della lettera di dimissione.",
    )
    app.state.demo_identities = demo_identities

    @app.middleware("http")
    async def request_context(request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request.state.request_id = (
            supplied if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", supplied) else str(uuid4())
        )
        start = perf_counter()
        try:
            result = await call_next(request)
        except Exception as exc:
            logger.error(
                json.dumps(
                    {
                        "event": "request_failed",
                        "request_id": request.state.request_id,
                        "error_type": type(exc).__name__,
                    }
                )
            )
            result = problem(request, 500, "ERRORE_INTERNO", "Errore interno del servizio.")
        result.headers["X-Request-ID"] = request.state.request_id
        result.headers["Cache-Control"] = "no-store"
        result.headers["X-Content-Type-Options"] = "nosniff"
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "route": getattr(request.scope.get("route"), "path", "unmatched"),
                    "status": result.status_code,
                    "demo_subject": getattr(request.state, "demo_subject", None),
                    "duration_ms": round((perf_counter() - start) * 1000, 2),
                }
            )
        )
        return result

    @app.exception_handler(BackendError)
    async def backend_error(request, exc):
        return problem(request, exc.status, exc.code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Non includere valori ricevuti o contenuti clinici nella risposta.
        fields = sorted({".".join(map(str, error["loc"])) for error in exc.errors()})
        return problem(
            request, 422, "RICHIESTA_NON_VALIDA", "Controllare i parametri: " + ", ".join(fields)
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        result = problem(
            request,
            exc.status_code,
            "AUTENTICAZIONE_RICHIESTA" if exc.status_code == 401 else "ERRORE_HTTP",
            "Credenziali locali assenti o non valide."
            if exc.status_code == 401
            else "Risorsa o operazione non disponibile.",
        )
        if exc.headers:
            result.headers.update(exc.headers)
        return result

    @app.get("/health/live", tags=["Servizio"])
    def live():
        return {"stato": "attivo"}

    @app.get("/health/ready", tags=["Servizio"])
    async def ready(request: Request):
        try:
            async with request.app.state.database.connection(read_only=True) as connection:
                await connection.execute("SELECT id, payload FROM composition_contexts LIMIT 0")
                await connection.execute(
                    "SELECT id, context_id, payload FROM composition_sources LIMIT 0"
                )
        except (DatabaseError, PoolTimeout):
            return JSONResponse(status_code=503, content={"stato": "non_pronto"})
        return {"stato": "pronto"}

    app.include_router(router)
    return app
