"""Adattatore HTTP del generatore CLI, con snapshot e stato durevole su disco.

Un processo Uvicorn, una generazione attiva. I POST ripetuti restituiscono lo
stesso lavoro; dopo un riavvio quelli incompleti diventano interrotti.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

from codice_tesi.application import run_application
from codice_tesi.config import ModelConfig
from codice_tesi.demo_access import DEMO_IDENTITIES, DemoIdentity
from codice_tesi.discharge.client import PacketClient
from codice_tesi.discharge.facts import extract_facts
from codice_tesi.discharge.limits import (
    EvidenceReadError,
    ExecutionTimeoutError,
    FinalPhaseToolCallError,
    ModelInactivityError,
    ToolCallValidationError,
)
from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.provenance import EvidenceCatalog, TraceResult, build_catalog
from codice_tesi.http_auth import require_identity
from codice_tesi.web_api.drafts import EditorBody, preview_sections

logger = logging.getLogger(__name__)
Identity = Annotated[DemoIdentity, Depends(require_identity)]


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_id: str


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thinking: bool = True
    tools_enabled: bool = False
    timeout_seconds: int | None = Field(default=None, ge=30)


def write_json(path: Path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def create_app(*, output_dir: Path | None = None, backend_url: str | None = None):
    output = (output_dir or Path(os.getenv("WEB_OUTPUT_DIR", "output/dimissioni"))).resolve()
    backend = backend_url or os.getenv("APP_BACKEND_URL", "http://127.0.0.1:8001")
    sessions = output / "web-sessions"
    tasks: dict[str, asyncio.Task] = {}
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        sessions.mkdir(parents=True, exist_ok=True)
        for path in sessions.glob("*/state.json"):
            state = json.loads(path.read_text())
            if state["status"] in {"running", "cancelling"}:
                state.update(
                    status="interrupted", detail="Servizio riavviato durante la generazione."
                )
                write_json(path, state)
        yield
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    app = FastAPI(title="API interfaccia dimissioni", lifespan=lifespan)
    app.state.demo_identities = DEMO_IDENTITIES

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def session(session_id: UUID, identity: DemoIdentity):
        directory = sessions / str(session_id)
        try:
            packet = CompositionPacket.model_validate_json((directory / "packet.json").read_text())
        except FileNotFoundError:
            raise HTTPException(404, "Preparazione non trovata.") from None
        if packet.context.id not in identity.context_ids:
            raise HTTPException(403, "Degenza fuori assegnazione.")
        return directory, packet

    def state_at(directory):
        return json.loads((directory / "state.json").read_text())

    def editor_at(directory):
        path = directory / "editor.json"
        return json.loads(path.read_text()) if path.exists() else {"revision": 0, "sections": {}}

    def job_at(directory):
        state = state_at(directory)
        preview = directory / "preview.json"
        if preview.exists():
            state["preview"] = json.loads(preview.read_text())
        if state["status"] == "completed":
            state["result"] = json.loads((directory / "result.json").read_text())
        return state

    def catalog_at(directory, packet):
        path = directory / "catalog.json"
        if path.exists():
            return EvidenceCatalog.model_validate_json(path.read_text())
        result_path = directory / "result.json"
        catalog = (
            TraceResult.model_validate_json(result_path.read_text()).catalog
            if result_path.exists()
            else build_catalog(packet)
        )
        write_json(path, catalog.model_dump(mode="json"))
        return catalog

    async def generate(directory, packet, options):
        raw_text = ""
        published_text = None
        published_at = 0
        response_revision = 0

        def publish_preview(*, force=False):
            nonlocal published_text, published_at
            if raw_text == published_text or (not force and monotonic() - published_at < 0.25):
                return
            write_json(
                directory / "preview.json",
                {
                    "response_revision": response_revision,
                    "sections": preview_sections(raw_text, packet.template),
                },
            )
            published_text, published_at = raw_text, monotonic()

        def receive_text(text, reset):
            nonlocal raw_text, response_revision
            if reset:
                raw_text = ""
                response_revision += 1
            raw_text += text
            publish_preview(force=reset or not published_text)

        def update(**values):
            publish_preview(force=True)
            state = state_at(directory)
            state.update(values, updated_at=datetime.now(UTC).isoformat())
            write_json(directory / "state.json", state)

        try:
            artifacts = await run_application(
                backend_url=backend,
                output_dir=output,
                packet=packet,
                console=False,
                catalog=catalog_at(directory, packet),
                config=ModelConfig(
                    thinking=options.thinking,
                    tools_enabled=options.tools_enabled,
                    total_timeout=options.timeout_seconds,
                ),
                on_progress=lambda progress: update(**progress),
                on_text=receive_text,
            )
            # Il solo risultato strutturato pubblicato dopo i controlli esistenti.
            result = TraceResult.model_validate_json(
                (artifacts / "bozza_strutturata.json").read_text()
            )
            if result.binding.context_id != packet.context.id:
                raise ValueError("Risultato fuori contesto.")
            write_json(directory / "result.json", result.model_dump(mode="json"))
            update(status="completed", phase="completata", detail="Bozza generata, da revisionare.")
        except asyncio.CancelledError:
            update(
                status="interrupted",
                phase="interrotta",
                detail=(
                    "Generazione interrotta. Chiusura client eseguita; "
                    "arresto remoto non attestato."
                ),
            )
            raise
        except ModelInactivityError as exc:
            update(
                status="failed",
                phase="senza_risposta",
                failure_code="model_inactivity_timeout",
                detail=(
                    f"Il modello non ha inviato progressi per {exc.seconds:g} secondi. "
                    "Il serving potrebbe essere occupato o la richiesta in coda. "
                    "Verificare eventuali generazioni già attive, anche da CLI, "
                    "prima di preparare un nuovo tentativo."
                ),
            )
        except ExecutionTimeoutError:
            update(
                status="failed",
                phase="scaduta",
                failure_code="total_timeout",
                detail="Durata massima raggiunta. Risposta parziale conservata negli artefatti.",
            )
        except FinalPhaseToolCallError:
            update(
                status="failed",
                phase="tool_in_fase_finale",
                failure_code="tool_call_after_final",
                detail=(
                    "Il modello ha richiesto un tool dopo la chiusura delle verifiche. "
                    "L'operazione non è stata eseguita e non sono state avviate altre inferenze. "
                    "Risposta parziale conservata negli artefatti."
                ),
            )
        except EvidenceReadError as exc:
            update(
                status="failed",
                phase="lettura_fonti_fallita",
                failure_code="tool_read_" + exc.code,
                detail=(
                    f"La verifica documentale richiesta dal modello non è riuscita: {exc} "
                    "Nessuna bozza completa pubblicata."
                ),
            )
        except ToolCallValidationError as exc:
            update(
                status="failed",
                phase="richiesta_tool_non_valida",
                failure_code="tool_call_invalid",
                detail=(
                    f"Il modello ha chiesto una verifica tramite {exc.tool_name} "
                    "con nome o parametri non validi. La correzione consentita non è riuscita. "
                    "Nessuna bozza completa pubblicata; dettagli del tentativo negli artefatti."
                ),
            )
        except UnexpectedModelBehavior:
            update(
                status="failed",
                phase="risposta_non_valida",
                failure_code="model_protocol_error",
                detail=(
                    "Il server ha risposto, ma il modello ha prodotto una risposta "
                    "o una chiamata ai tool non valida. "
                    "La generazione è stata fermata; nessuna bozza completa pubblicata."
                ),
            )
        except UsageLimitExceeded:
            update(
                status="failed",
                phase="budget_esaurito",
                failure_code="model_usage_limit",
                detail=(
                    "Raggiunto il budget di richieste, chiamate ai tool o token. "
                    "Il testo parziale disponibile resta consultabile; "
                    "non è una bozza completata."
                ),
            )
        except Exception as exc:
            logger.error("Generazione fallita: %s", type(exc).__name__)
            update(
                status="failed",
                detail="Generazione non completata. Verificare il serving LLM e gli artefatti.",
            )

    @app.get("/health/live")
    async def live():
        return {"stato": "attivo"}

    @app.post("/api/v1/preparazioni", status_code=201)
    async def prepare(body: PrepareBody, identity: Identity):
        if body.context_id not in identity.context_ids:
            raise HTTPException(403, "Degenza fuori assegnazione.")
        try:
            packet = await PacketClient(backend, token=identity.token).fetch(
                seed=0, context_id=body.context_id
            )
        except ValueError:
            raise HTTPException(502, "Impossibile recuperare la degenza dal backend.") from None
        session_id = str(uuid4())
        directory = sessions / session_id
        directory.mkdir(parents=True)
        write_json(directory / "packet.json", packet.model_dump(mode="json"))
        write_json(directory / "catalog.json", build_catalog(packet).model_dump(mode="json"))
        state = {"id": session_id, "context_id": packet.context.id, "status": "prepared"}
        write_json(directory / "state.json", state)
        return state

    @app.get("/api/v1/preparazioni/{session_id}")
    async def prepared(session_id: UUID, identity: Identity):
        directory, packet = session(session_id, identity)
        catalog = catalog_at(directory, packet)
        return {
            **job_at(directory),
            "editor": editor_at(directory),
            "packet": packet.model_dump(mode="json"),
            "catalog": catalog.model_dump(mode="json"),
            "facts": extract_facts(catalog).model_dump(mode="json"),
        }

    @app.post("/api/v1/preparazioni/{session_id}/modifiche")
    async def save_edits(session_id: UUID, identity: Identity, body: EditorBody):
        directory, packet = session(session_id, identity)
        if not body.sections.keys() <= {section.id for section in packet.template.sections}:
            raise HTTPException(422, "Sezione non presente nel template della preparazione.")
        async with lock:
            editor = editor_at(directory)
            if body.revision != editor["revision"]:
                raise HTTPException(
                    409,
                    "La bozza è stata modificata in un'altra scheda. "
                    "Copia le tue modifiche prima di ricaricare la pagina.",
                )
            editor = {
                "revision": body.revision + 1,
                "sections": body.sections,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            write_json(directory / "editor.json", editor)
            return editor

    @app.post("/api/v1/preparazioni/{session_id}/genera", status_code=202)
    async def start(session_id: UUID, identity: Identity, body: StartBody | None = None):
        directory, packet = session(session_id, identity)
        async with lock:
            state = state_at(directory)
            if state["status"] in {"running", "cancelling", "completed", "failed", "interrupted"}:
                return state
            if tasks:
                raise HTTPException(409, "Una generazione è già in corso. Riprovare al termine.")
            options = body or StartBody()
            state.update(
                status="running",
                phase="avvio",
                detail="Generazione in corso…",
                started_at=datetime.now(UTC).isoformat(),
                options=options.model_dump(),
            )
            write_json(directory / "state.json", state)
            task = asyncio.create_task(generate(directory, packet, options))
            tasks[str(session_id)] = task
            task.add_done_callback(lambda _: tasks.pop(str(session_id), None))
            return state

    @app.post("/api/v1/preparazioni/{session_id}/interrompi")
    async def cancel(session_id: UUID, identity: Identity):
        directory, _ = session(session_id, identity)
        async with lock:
            state = state_at(directory)
            if state["status"] not in {"running", "cancelling"}:
                return state
            state.update(status="cancelling", detail="Interruzione richiesta…")
            write_json(directory / "state.json", state)
            task = tasks.get(str(session_id))
            if task and not task.cancelling():
                task.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        state = state_at(directory)
        if state["status"] == "cancelling":
            # Include cancellazioni richieste prima dell'avvio della coroutine.
            state.update(status="interrupted", phase="interrotta", detail="Generazione interrotta.")
            write_json(directory / "state.json", state)
        return state

    @app.get("/api/v1/preparazioni/{session_id}/stato")
    async def status(session_id: UUID, identity: Identity):
        directory, _ = session(session_id, identity)
        return job_at(directory)

    @app.get("/api/v1/preparazioni/{session_id}/stream")
    async def stream(session_id: UUID, identity: Identity, request: Request):
        directory, _ = session(session_id, identity)

        async def events():
            previous = None
            last_sent = monotonic()
            while not await request.is_disconnected():
                state = job_at(directory)
                data = json.dumps(state, ensure_ascii=False)
                if data != previous:
                    yield f"data: {data}\n\n"
                    previous, last_sent = data, monotonic()
                elif monotonic() - last_sent >= 10:
                    yield ": keep-alive\n\n"
                    last_sent = monotonic()
                if state["status"] not in {"running", "cancelling"}:
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/api/v1/preparazioni/{session_id}/fonti/{source_id}")
    async def source(session_id: UUID, source_id: str, identity: Identity):
        directory, packet = session(session_id, identity)
        result_path = directory / "result.json"
        catalog = (
            TraceResult.model_validate_json(result_path.read_text()).catalog
            if result_path.exists()
            else catalog_at(directory, packet)
        )
        for item in catalog.sources:
            if item.id == source_id:
                return JSONResponse(
                    {
                        "source": item.model_dump(mode="json"),
                        "evidences": [
                            evidence.model_dump(mode="json")
                            for evidence in catalog.evidences
                            if evidence.source_id == source_id
                        ],
                    }
                )
        raise HTTPException(404, "Fonte non presente nel catalogo della degenza.")

    return app
