"""API di lettura: dati clinici → database → pacchetto della lettera."""

import json
import logging
import re
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException

from codice_tesi.backend.database import BackendSettings, Database
from codice_tesi.backend.repository import BackendError, read_packet
from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.template import LetterTemplate, load_template

logger = logging.getLogger("clinical_backend")
router = APIRouter(prefix="/api/v1/compilazione", tags=["Compilazione"])


@router.get("/template", response_model=LetterTemplate)
async def active_template():
    """Espone il template Campus incluso nell'applicazione."""
    return load_template()


@router.get("/avvio-test", response_model=CompositionPacket)
async def bootstrap(request: Request, seed: Annotated[int, Query(ge=0)]):
    """Seleziona un contesto con seed riproducibile e include tutte le sue note."""
    return await read_packet(request.app.state.database, seed=seed)


@router.get("/contesti/{context_id}/pacchetto", response_model=CompositionPacket)
async def packet(request: Request, context_id: str):
    """Legge il contesto richiesto, le sue fonti e il template Campus."""
    return await read_packet(request.app.state.database, context_id=context_id)


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


def create_app(settings: BackendSettings | None = None) -> FastAPI:
    settings = settings or BackendSettings()

    @asynccontextmanager
    async def lifespan(app):
        async with Database(settings) as database:
            app.state.database = database
            yield

    app = FastAPI(
        title="Backend documentale clinico",
        version="0.3.0",
        lifespan=lifespan,
        description="Contesti e fonti per la compilazione della lettera di dimissione.",
    )

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
            request, exc.status_code, "ERRORE_HTTP", "Risorsa o operazione non disponibile."
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
