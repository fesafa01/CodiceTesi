"""Superficie HTTP della compilazione; nessuna logica di importazione o inferenza."""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from codice_tesi.backend.composition_repository import read_packet
from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.template import LetterTemplate, load_template

router = APIRouter(prefix="/api/v1/compilazione", tags=["Compilazione deterministica"])


@router.get("/template", response_model=LetterTemplate)
async def active_template():
    """Espone il contratto canonico senza interrogare le tabelle o richiedere un dataset."""
    return load_template()


@router.get("/avvio-test", response_model=CompositionPacket)
async def bootstrap(request: Request, seed: Annotated[int, Query(ge=0)]):
    """Seleziona il caso del test e lo associa al template attivo in uno snapshot."""
    return await read_packet(request.app.state.database, seed=seed)


@router.get("/contesti/{context_id}/pacchetto", response_model=CompositionPacket)
async def packet(request: Request, context_id: str):
    """Prepara il contesto già aperto; non effettua alcuna selezione casuale."""
    return await read_packet(request.app.state.database, context_id=context_id)
