"""API cliniche di sola lettura con stato del simulatore separato dalla superficie HTTP."""

import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import Response as HTTPResponse
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from codice_tesi.backend.config import BackendSettings
from codice_tesi.backend.database import Database
from codice_tesi.backend.models import Episode
from codice_tesi.backend.repository import (
    Channel,
    ClinicalError,
    ClinicalRepository,
    Scope,
    open_repository,
)
from codice_tesi.backend.schemas import (
    AdmissionContext,
    ChangeRead,
    CoverageRead,
    EpisodeRead,
    Metadata,
    Pagination,
    PatientRead,
    Problem,
    Response,
    ResultRead,
    SourceDetail,
    SourceRead,
    TemplateRead,
    UnitRead,
)

logger = logging.getLogger("clinical_backend")


class PageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limite: int = Field(default=50, ge=1, le=100)
    cursore: str | None = Field(default=None, max_length=2048)


class SourceQuery(PageQuery):
    ambito: Scope = "episodio"
    canale: Channel | None = None
    categoria: Literal["registrazione", "documento"] | None = None
    tipo: str | None = Field(default=None, min_length=1, max_length=60)
    q: str | None = Field(default=None, min_length=1, max_length=200)
    dal: AwareDatetime | None = None
    al: AwareDatetime | None = None


class AdmissionQuery(PageQuery):
    reparto_id: str | None = Field(default=None, max_length=80)
    stato: Literal["in_degenza", "dimesso"] | None = None


class ResultQuery(PageQuery):
    tipo: Literal["laboratorio", "parametro_vitale"] | None = None


async def repository(
    request: Request,
    expected_revision: Annotated[int | None, Header(alias="X-Clinical-Revision", ge=1)] = None,
) -> AsyncIterator[ClinicalRepository]:
    async with open_repository(request.app.state.database) as repo:
        request.state.snapshot = repo.view
        if expected_revision is not None and expected_revision != repo.view.revision:
            raise ClinicalError(
                409, "REVISIONE_CAMBIATA", "La revisione è cambiata: aggiornare le fonti."
            )
        yield repo


Repo = Annotated[ClinicalRepository, Depends(repository)]


def response(request: Request, repo: ClinicalRepository, data, cursor=None, *, paginated=False):
    view = repo.view
    return Response(
        meta=Metadata(
            scenario=view.scenario,
            esecuzione_id=view.run_id,
            revisione=view.revision,
            istante=view.time,
            versione_dataset=view.dataset_version,
            dataset_caricato=view.scenario is not None,
            request_id=request.state.request_id,
            canali_non_disponibili=list(view.unavailable_channels),
        ),
        dati=data,
        paginazione=Pagination(prossimo_cursore=cursor, completa=cursor is None)
        if paginated
        else None,
    )


def episode_read(row: Episode) -> EpisodeRead:
    return EpisodeRead(
        id=row.id,
        paziente_id=row.patient_id,
        tipo=row.kind,
        stato=row.status,
        ingresso_il=row.started_at,
        nosologico=row.nosological_id,
        unita_clinica_id=row.unit_id,
        unita_degenza_id=row.ward_id,
        letto=row.bed,
        dimissione_prevista_il=row.planned_discharge,
        dimissione_effettiva_il=row.discharged_at,
    )


router = APIRouter(
    prefix="/api/v1",
    responses={
        status: {
            "description": description,
            "content": {"application/problem+json": {"schema": Problem.model_json_schema()}},
        }
        for status, description in {
            400: "Cursore o revisione non validi.",
            404: "Risorsa non disponibile nel contesto richiesto.",
            409: "Revisione, esecuzione o cursore cambiati.",
            422: "Parametri della richiesta non validi.",
            500: "Errore interno del servizio.",
            503: "Database o canale temporaneamente non disponibile.",
        }.items()
    },
)


@router.get("/reparti", response_model=Response[list[UnitRead]], tags=["Contesto"])
async def units(request: Request, repo: Repo, page: Annotated[PageQuery, Query()]):
    rows, cursor = await repo.units(limit=page.limite, cursor=page.cursore)
    return response(
        request,
        repo,
        [UnitRead(id=r.id, nome=r.name, tipo=r.kind) for r in rows],
        cursor,
        paginated=True,
    )


@router.get("/ricoveri", response_model=Response[list[EpisodeRead]], tags=["Contesto"])
async def admissions(request: Request, repo: Repo, page: Annotated[AdmissionQuery, Query()]):
    rows, cursor = await repo.admissions(
        unit=page.reparto_id, status=page.stato, limit=page.limite, cursor=page.cursore
    )
    return response(request, repo, [episode_read(row) for row in rows], cursor, paginated=True)


@router.get("/ricoveri/{ricovero_id}", response_model=Response[AdmissionContext], tags=["Contesto"])
async def admission(ricovero_id: str, request: Request, repo: Repo):
    row = await repo.admission(ricovero_id)
    patient = await repo.patient(row.patient_id)
    return response(
        request,
        repo,
        AdmissionContext(
            ricovero=episode_read(row),
            paziente=PatientRead(
                id=patient.id,
                nome=patient.given_name,
                cognome=patient.family_name,
                data_nascita=patient.birth_date,
            ),
        ),
    )


async def source_list(ricovero_id, request, repo, query, category):
    admission = await repo.admission(ricovero_id)
    rows, cursor = await repo.list_sources(
        admission,
        category=category,
        scope=query.ambito,
        channel=query.canale,
        kind=query.tipo,
        q=query.q,
        after_date=query.dal,
        before_date=query.al,
        limit=query.limite,
        cursor=query.cursore,
    )
    return response(request, repo, rows, cursor, paginated=True)


@router.get(
    "/ricoveri/{ricovero_id}/fonti", response_model=Response[list[SourceRead]], tags=["Fonti"]
)
async def sources(
    ricovero_id: str, request: Request, repo: Repo, query: Annotated[SourceQuery, Query()]
):
    return await source_list(ricovero_id, request, repo, query, query.categoria)


@router.get(
    "/ricoveri/{ricovero_id}/registrazioni",
    response_model=Response[list[SourceRead]],
    tags=["Fonti"],
)
async def records(
    ricovero_id: str, request: Request, repo: Repo, query: Annotated[SourceQuery, Query()]
):
    if query.categoria not in {None, "registrazione"}:
        raise ClinicalError(422, "CATEGORIA_NON_VALIDA", "Questo endpoint espone registrazioni.")
    return await source_list(ricovero_id, request, repo, query, "registrazione")


@router.get(
    "/ricoveri/{ricovero_id}/documenti", response_model=Response[list[SourceRead]], tags=["Fonti"]
)
async def documents(
    ricovero_id: str, request: Request, repo: Repo, query: Annotated[SourceQuery, Query()]
):
    if query.categoria not in {None, "documento"}:
        raise ClinicalError(422, "CATEGORIA_NON_VALIDA", "Questo endpoint espone documenti.")
    return await source_list(ricovero_id, request, repo, query, "documento")


@router.get(
    "/ricoveri/{ricovero_id}/fonti/{fonte_id}",
    response_model=Response[SourceDetail],
    tags=["Fonti"],
)
async def source(
    ricovero_id: str,
    fonte_id: str,
    request: Request,
    repo: Repo,
    versione: Annotated[int | None, Query(ge=1)] = None,
    canale: Channel | None = None,
):
    admission = await repo.admission(ricovero_id)
    row, version, current_status = await repo.source(admission, fonte_id, versione, canale)
    download = (
        f"/api/v1/ricoveri/{ricovero_id}/documenti/{row.id}/contenuto?versione={version.version}"
        if version.pdf
        else None
    )
    detail = SourceDetail(
        **(await repo.describe(row, version, current_status)).model_dump(),
        testo=version.text,
        sezioni=version.sections,
        contenuto_url=download,
        tipo_contenuto="application/pdf" if version.pdf else None,
        sha256=version.sha256,
        stato_estrazione=version.extraction_status,
        metodo_estrazione=version.extraction_method,
    )
    return response(request, repo, detail)


@router.get(
    "/ricoveri/{ricovero_id}/documenti/{documento_id}/contenuto",
    tags=["Documenti"],
    response_class=HTTPResponse,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def content(
    ricovero_id: str,
    documento_id: str,
    request: Request,
    repo: Repo,
    versione: Annotated[int | None, Query(ge=1)] = None,
    canale: Channel | None = None,
):
    row, version, current_status = await repo.source(
        await repo.admission(ricovero_id), documento_id, versione, canale
    )
    if row.category != "documento" or not version.pdf:
        raise ClinicalError(404, "DOCUMENTO_NON_TROVATO", "Contenuto documentale non disponibile.")
    headers = {
        "ETag": f'"{version.sha256}"',
        "Content-Disposition": f'inline; filename="{row.id}_v{version.version}.pdf"',
        "X-Source-Version": str(version.version),
        "X-Source-Current-Status": current_status,
    }
    if request.headers.get("if-none-match") == headers["ETag"]:
        return HTTPResponse(status_code=304, headers=headers)
    return HTTPResponse(version.pdf, media_type="application/pdf", headers=headers)


@router.get(
    "/ricoveri/{ricovero_id}/risultati",
    response_model=Response[list[ResultRead]],
    tags=["Risultati"],
)
async def results(
    ricovero_id: str, request: Request, repo: Repo, query: Annotated[ResultQuery, Query()]
):
    rows, cursor = await repo.results(
        await repo.admission(ricovero_id), kind=query.tipo, limit=query.limite, cursor=query.cursore
    )
    data = [
        ResultRead(
            id=f"RES-{source.id}-{result.ordinal}",
            tipo=result.kind,
            esame=result.name,
            valore=result.value,
            unita=result.unit,
            codice_unita=result.unit_code,
            codice=result.code,
            sistema_codifica=result.code_system,
            data_clinica=result.clinical_at,
            fonte=await repo.describe(source, version),
        )
        for source, version, result in rows
    ]
    return response(request, repo, data, cursor, paginated=True)


@router.get(
    "/ricoveri/{ricovero_id}/moduli",
    response_model=Response[list[CoverageRead]],
    tags=["Copertura"],
)
async def modules(ricovero_id: str, request: Request, repo: Repo):
    rows = await repo.coverage(await repo.admission(ricovero_id))
    return response(request, repo, [CoverageRead(**row) for row in rows])


@router.get(
    "/ricoveri/{ricovero_id}/aggiornamenti",
    response_model=Response[list[ChangeRead]],
    tags=["Versioni"],
)
async def updates(
    ricovero_id: str, request: Request, repo: Repo, da_revisione: Annotated[int, Query(ge=1)]
):
    return response(
        request, repo, await repo.changes(await repo.admission(ricovero_id), da_revisione)
    )


@router.get(
    "/modelli/{modello_id}",
    response_model=Response[TemplateRead],
    tags=["Modelli storici"],
    deprecated=True,
    description=("Metadati storici importati. Contratto attivo: /api/v1/compilazione/template."),
)
async def template(modello_id: str, request: Request, repo: Repo):
    """Legge una descrizione storica del dataset, mai usata come template della stesura."""
    row = await repo.template(modello_id)
    return response(
        request,
        repo,
        TemplateRead(
            id=row["id"],
            versione=row["version"],
            unita_clinica_id=row["unit_id"],
            campi=row["fields"],
        ),
    )


def problem(request, status, code, detail):
    if "/fhir/" in request.url.path:
        from codice_tesi.backend.fhir_adapter import outcome
        from codice_tesi.backend.fhir_routes import FHIRResponse

        return FHIRResponse(outcome(status, detail), status_code=status)
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
        title="Backend clinico sintetico",
        version="0.2.0",
        lifespan=lifespan,
        description=(
            "API di sola lettura del simulatore clinico. "
            "Nessun endpoint di prescrizione, firma o controllo degli scenari."
        ),
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
        view = getattr(request.state, "snapshot", None)
        if view:
            result.headers["X-Clinical-Revision"] = str(view.revision)
            if view.run_id is not None:
                result.headers["X-Simulation-Run"] = view.run_id
            result.headers["X-Clinical-Unavailable-Channels"] = ",".join(view.unavailable_channels)
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

    @app.exception_handler(ClinicalError)
    async def clinical_error(request, exc):
        return problem(request, exc.status, exc.code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Non includere valori ricevuti, contenuti clinici o stack trace negli errori.
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
            async with open_repository(request.app.state.database):
                pass
        except (DatabaseError, PoolTimeout, ClinicalError):
            return JSONResponse(status_code=503, content={"stato": "non_pronto"})
        return {"stato": "pronto"}

    from codice_tesi.backend.composition_routes import router as composition_router
    from codice_tesi.backend.fhir_routes import create_router

    app.include_router(router)
    app.include_router(composition_router)
    app.include_router(create_router(repository))
    return app
