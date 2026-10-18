"""Superficie FHIR read/search di un contesto clinico selezionato."""

from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from codice_tesi.backend import fhir_adapter as fhir
from codice_tesi.backend.repository import (
    Channel,
    ClinicalError,
    ClinicalRepository,
    Scope,
)


class FHIRResponse(Response):
    media_type = fhir.FHIR_MEDIA_TYPE

    def __init__(self, resource, status_code=200, media_type=None):
        super().__init__(
            resource.model_dump_json().encode(),
            status_code=status_code,
            media_type=media_type or self.media_type,
            headers={"Vary": "Accept"},
        )


JSON_MEDIA_TYPES = (fhir.FHIR_MEDIA_TYPE, "application/json", "text/json")


def negotiate_format(request: Request) -> str:
    """Solo JSON: una richiesta XML non deve ricevere un successo JSON silenzioso."""
    formats = request.query_params.getlist("_format")
    if len(formats) > 1:
        raise ClinicalError(400, "FORMATO_AMBIGUO", "Specificare un solo _format.")
    if formats:
        if formats[0] == "json":
            return fhir.FHIR_MEDIA_TYPE
        if formats[0] in JSON_MEDIA_TYPES:
            return formats[0]
        raise ClinicalError(406, "FORMATO_NON_SUPPORTATO", "Disponibile soltanto FHIR JSON R4B.")

    # Precedenza HTTP: un rifiuto specifico (q=0) prevale sui wildcard.
    ranges = []
    for item in request.headers.get("accept", "*/*").split(","):
        media, *parameters = item.strip().split(";")
        params = {}
        for parameter in parameters:
            key, separator, value = parameter.strip().partition("=")
            if not separator:
                raise ClinicalError(400, "ACCEPT_NON_VALIDO", "Header Accept non valido.")
            params[key.lower()] = value.strip().strip('"')
        try:
            quality = Decimal(params.pop("q", "1"))
            if not quality.is_finite() or not 0 <= quality <= 1:
                raise InvalidOperation
        except InvalidOperation:
            raise ClinicalError(400, "ACCEPT_NON_VALIDO", "Qualità Accept non valida.") from None
        ranges.append((media.lower(), params, quality))

    choices = []
    for preference, candidate in enumerate(JSON_MEDIA_TYPES):
        matches = []
        for media, params, quality in ranges:
            if media not in {candidate, candidate.split("/")[0] + "/*", "*/*"}:
                continue
            if any(
                (key == "fhirversion" and value != "4.3")
                or (key == "charset" and value.lower() != "utf-8")
                or key not in {"fhirversion", "charset"}
                for key, value in params.items()
            ):
                continue
            specificity = (2 - media.count("*"), len(params))
            matches.append((specificity, quality))
        if matches:
            specificity = max(score for score, _ in matches)
            quality = max(q for score, q in matches if score == specificity)
            if quality > 0:
                choices.append((quality, -preference, candidate))
    if choices:
        return max(choices)[2]
    raise ClinicalError(406, "FORMATO_NON_SUPPORTATO", "Disponibile soltanto FHIR JSON R4B 4.3.")


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(default=50, ge=1, le=100, alias="_count")
    cursor: str | None = Field(default=None, max_length=2048)
    ambito: Scope = "episodio"
    canale: Channel | None = None
    format_: str | None = Field(default=None, alias="_format")


def create_router(dependency):
    Repo = Annotated[ClinicalRepository, Depends(dependency)]
    Format = Annotated[str, Depends(negotiate_format)]
    router = APIRouter(
        prefix="/api/v1/ricoveri/{ricovero_id}/fhir",
        tags=["FHIR R4B"],
        default_response_class=FHIRResponse,
    )

    @router.get("/metadata")
    async def metadata(ricovero_id: str, request: Request, media_type: Format, repo: Repo):
        await repo.admission(ricovero_id)
        base = str(request.url).rsplit("/metadata", 1)[0]
        return FHIRResponse(fhir.capability(base), media_type=media_type)

    @router.get("/{resource_type}/{resource_id}")
    async def read(
        ricovero_id: str,
        resource_type: fhir.ResourceType,
        resource_id: str,
        request: Request,
        media_type: Format,
        repo: Repo,
    ):
        result = await fhir.resource(
            repo,
            await repo.admission(ricovero_id),
            resource_type,
            resource_id,
            str(request.base_url).rstrip("/"),
        )
        return FHIRResponse(result, media_type=media_type)

    @router.get("/{resource_type}")
    async def search(
        ricovero_id: str,
        resource_type: fhir.ResourceType,
        request: Request,
        media_type: Format,
        repo: Repo,
        query: Annotated[SearchQuery, Query()],
    ):
        admission = await repo.admission(ricovero_id)
        repo._check_channel(query.canale)
        origin = str(request.base_url).rstrip("/")
        next_cursor = None
        if resource_type in {"Patient", "Encounter"}:
            rows, next_cursor = await repo.context_page(
                admission,
                resource_type,
                scope=query.ambito,
                channel=query.canale,
                limit=query.count,
                cursor=query.cursor,
            )
            mapper = fhir.patient if resource_type == "Patient" else fhir.encounter
            resources = [mapper(row) for row in rows]
        elif resource_type == "Observation":
            rows, next_cursor = await repo.results(
                admission,
                kind=None,
                limit=query.count,
                cursor=query.cursor,
                scope=query.ambito,
                channel=query.canale,
            )
            resources = [fhir.observation(s, v, r) for s, v, r in rows]
        else:
            diagnostic = resource_type == "DiagnosticReport"
            rows, next_cursor = await repo.list_sources(
                admission,
                category="documento" if diagnostic else None,
                scope=query.ambito,
                channel=query.canale,
                kind=None,
                q=None,
                after_date=None,
                before_date=None,
                limit=query.count,
                cursor=query.cursor,
                diagnostic_only=diagnostic,
                with_text=True,
                raw=True,
            )
            resources = []
            for s, v in rows:
                resources.append(
                    await fhir.diagnostic_report(repo, s, v, origin, ricovero_id)
                    if diagnostic
                    else fhir.document_reference(s, v, origin, ricovero_id)
                )
        next_url = (
            str(request.url.include_query_params(cursor=next_cursor)) if next_cursor else None
        )
        base = f"{origin}/api/v1/ricoveri/{ricovero_id}/fhir"
        return FHIRResponse(
            fhir.bundle(resources, str(request.url), next_url, repo.view.time, base),
            media_type=media_type,
        )

    return router
