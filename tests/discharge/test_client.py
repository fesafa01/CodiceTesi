"""Il confine HTTP non può cambiare il caso o sostituire il template applicativo."""

import asyncio
import json
from io import BytesIO
from urllib.error import HTTPError

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


def test_client_sends_only_selected_local_credential(monkeypatch, packet_factory):
    packet = packet_factory("triage")
    calls = []

    def fetch(request, **kwargs):
        calls.append(request)
        return BytesIO(packet.model_dump_json().encode())

    monkeypatch.setattr(client, "urlopen", fetch)
    monkeypatch.setenv("APP_MIMIC_TOKEN", "demo-medico-a")
    asyncio.run(client.PacketClient("http://test").fetch(seed=42, context_id=None))
    asyncio.run(
        client.PacketClient("http://test", token="demo-medico-b").fetch(seed=42, context_id=None)
    )
    asyncio.run(client.PacketClient("http://test", token="").fetch(seed=42, context_id=None))
    assert [request.get_header("Authorization") for request in calls] == [
        "Bearer demo-medico-a",
        "Bearer demo-medico-b",
        None,
    ]
    assert all("demo-" not in request.full_url for request in calls)


@pytest.mark.parametrize("status", [401, 403])
def test_access_error_is_actionable_and_does_not_echo_token(monkeypatch, status):
    def fetch(request, **kwargs):
        raise HTTPError(request.full_url, status, "denied", {}, None)

    monkeypatch.setattr(client, "urlopen", fetch)
    with pytest.raises(ValueError, match=f"Backend HTTP {status}") as error:
        asyncio.run(
            client.PacketClient("http://test", token="PRIVATE_TOKEN").fetch(
                seed=42, context_id=None
            )
        )
    assert "PRIVATE_TOKEN" not in str(error.value)
