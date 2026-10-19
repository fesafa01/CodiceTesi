"""Il confine HTTP non può cambiare il caso o sostituire il template applicativo."""

import asyncio
import json
from io import BytesIO

import pytest

from codice_tesi.discharge import client


def test_backend_cannot_change_open_case(monkeypatch, packet_factory):
    packet = packet_factory("triage")
    monkeypatch.setattr(
        client, "urlopen", lambda *a, **k: BytesIO(packet.model_dump_json().encode())
    )
    with pytest.raises(ValueError, match="contesto diverso"):
        asyncio.run(client.PacketClient("http://test").fetch(seed=42, context_id="other"))


def test_backend_cannot_replace_template(monkeypatch, packet_factory):
    data = packet_factory("triage").model_dump(mode="json")
    data["template"]["sections"][0]["fields"][0]["instruction"] = "Istruzione sostituita"
    monkeypatch.setattr(client, "urlopen", lambda *a, **k: BytesIO(json.dumps(data).encode()))
    with pytest.raises(ValueError, match="Template"):
        asyncio.run(client.PacketClient("http://test").fetch(seed=42, context_id=None))
