"""Adattatore HTTP del generatore CLI, con snapshot e stato durevole su disco.

Un processo Uvicorn, una generazione attiva. I POST ripetuti restituiscono lo
stesso lavoro; dopo un riavvio quelli incompleti diventano interrotti.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from codice_tesi.application import run_application
from codice_tesi.backend.auth import require_identity
from codice_tesi.demo_access import DEMO_IDENTITIES, DemoIdentity
from codice_tesi.discharge.client import PacketClient
from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.provenance import TraceResult, build_catalog

logger = logging.getLogger(__name__)
Identity = Annotated[DemoIdentity, Depends(require_identity)]


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_id: str


def write_json(path: Path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def create_app(*, output_dir: Path | None = None, backend_url: str | None = None):
    output = (output_dir or Path(os.getenv("WEB_OUTPUT_DIR", "output/dimissioni"))).resolve()
    backend = backend_url or os.getenv("APP_BACKEND_URL", "http://127.0.0.1:8001")
    sessions = output / "web-sessions"
    tasks: set[asyncio.Task] = set()
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        sessions.mkdir(parents=True, exist_ok=True)
        for path in sessions.glob("*/state.json"):
            state = json.loads(path.read_text())
            if state["status"] == "running":
                state.update(
                    status="interrupted", detail="Servizio riavviato durante la generazione."
                )
                write_json(path, state)
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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

    async def generate(directory, packet):
        state = state_at(directory)
        try:
            artifacts = await run_application(
                backend_url=backend,
                output_dir=output,
                packet=packet,
                console=False,
            )
            # Il solo risultato strutturato pubblicato dopo i controlli esistenti.
            result = TraceResult.model_validate_json(
                (artifacts / "bozza_strutturata.json").read_text()
            )
            if result.binding.context_id != packet.context.id:
                raise ValueError("Risultato fuori contesto.")
            write_json(directory / "result.json", result.model_dump(mode="json"))
            state.update(status="completed", detail="Bozza generata, da revisionare.")
        except asyncio.CancelledError:
            state.update(status="interrupted", detail="Generazione interrotta dal servizio.")
            raise
        except Exception as exc:
            logger.error("Generazione fallita: %s", type(exc).__name__)
            state.update(
                status="failed",
                detail="Generazione non completata. Verificare il serving LLM e gli artefatti.",
            )
        finally:
            write_json(directory / "state.json", state)

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
        state = {"id": session_id, "context_id": packet.context.id, "status": "prepared"}
        write_json(directory / "state.json", state)
        return state

    @app.get("/api/v1/preparazioni/{session_id}")
    async def prepared(session_id: UUID, identity: Identity):
        directory, packet = session(session_id, identity)
        return {
            **state_at(directory),
            "packet": packet.model_dump(mode="json"),
            "catalog": build_catalog(packet).model_dump(mode="json"),
        }

    @app.post("/api/v1/preparazioni/{session_id}/genera", status_code=202)
    async def start(session_id: UUID, identity: Identity):
        directory, packet = session(session_id, identity)
        async with lock:
            state = state_at(directory)
            if state["status"] in {"running", "completed", "failed", "interrupted"}:
                return state
            if tasks:
                raise HTTPException(409, "Una generazione è già in corso. Riprovare al termine.")
            state.update(status="running", detail="Generazione in corso…")
            write_json(directory / "state.json", state)
            task = asyncio.create_task(generate(directory, packet))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return state

    @app.get("/api/v1/preparazioni/{session_id}/stato")
    async def status(session_id: UUID, identity: Identity):
        directory, _ = session(session_id, identity)
        state = state_at(directory)
        if state["status"] == "completed":
            state["result"] = json.loads((directory / "result.json").read_text())
        return state

    @app.get("/api/v1/preparazioni/{session_id}/fonti/{source_id}")
    async def source(session_id: UUID, source_id: str, identity: Identity):
        directory, packet = session(session_id, identity)
        result_path = directory / "result.json"
        catalog = (
            TraceResult.model_validate_json(result_path.read_text()).catalog
            if result_path.exists()
            else build_catalog(packet)
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
