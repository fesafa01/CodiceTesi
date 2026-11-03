"""Snapshot, lavori idempotenti, citazioni e accessi senza serving LLM reale."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from codice_tesi import application
from codice_tesi.demo_access import DemoIdentity
from codice_tesi.web_api.app import create_app


@pytest.fixture
def web_client(tmp_path, packet_factory, monkeypatch):
    calls = []
    packet = packet_factory("nota")

    async def fetch(self, **kwargs):
        calls.append(kwargs)
        return packet

    monkeypatch.setattr("codice_tesi.web_api.app.PacketClient.fetch", fetch)
    monkeypatch.setenv("TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("HARDWARE_ENABLED", "false")
    app = create_app(output_dir=tmp_path, backend_url="http://test")
    app.state.demo_identities = (
        DemoIdentity("test", "demo-test", frozenset({packet.context.id})),
        DemoIdentity("other", "demo-other", frozenset({"other"})),
    )
    with TestClient(app, headers={"Authorization": "Bearer demo-test"}) as client:
        yield client, calls, packet


def prepare(client):
    response = client.post("/api/v1/preparazioni", json={"context_id": "episode-x"})
    assert response.status_code == 201
    return "/api/v1/preparazioni/" + response.json()["id"]


def wait_result(client, path):
    for _ in range(150):
        result = client.get(path + "/stato").json()
        if result["status"] != "running":
            return result
        time.sleep(0.02)
    pytest.fail("Lavoro non terminato")


def test_preparation_fetches_once_and_exposes_exact_source_json(web_client):
    client, calls, packet = web_client
    path = prepare(client)
    assert len(calls) == 1
    prepared = client.get(path)
    assert prepared.headers["cache-control"] == "no-store"
    assert prepared.json()["packet"] == packet.model_dump(mode="json")
    assert prepared.json()["status"] == "prepared"
    source = client.get(path + "/fonti/source-0")
    assert source.json()["source"] == packet.sources[0].model_dump(mode="json")
    assert client.get(path + "/fonti/inventata").status_code == 404
    assert (
        client.get(
            path + "/fonti/source-0", headers={"Authorization": "Bearer demo-other"}
        ).status_code
        == 403
    )
    assert client.get(path, headers={"Authorization": "Bearer errato"}).status_code == 401
    assert (
        client.post(path + "/genera", headers={"Authorization": "Bearer demo-other"}).status_code
        == 403
    )
    assert client.post("/api/v1/preparazioni", json={"context_id": "other"}).status_code == 403
    assert len(calls) == 1


def test_generation_uses_preloaded_packet_and_publishes_validated_result(
    web_client, monkeypatch, letter_factory, response_factory, capsys
):
    client, calls, packet = web_client
    streams = []

    class Generator:
        usage = {}
        progress = {}

        def __init__(self, *args):
            pass

        async def stream(self, request):
            streams.append(request)
            await asyncio.sleep(0.05)
            yield response_factory(letter_factory())

    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    path = prepare(client)
    assert client.post(path + "/genera").status_code == 202
    assert client.post(path + "/genera").status_code == 202
    result = wait_result(client, path)
    assert result["status"] == "completed"
    assert result["result"]["binding"]["context_id"] == packet.context.id
    assert result["result"]["validation"]["technical"] == "passed"
    assert result["result"]["catalog"]["sources"][0]["text"] == packet.sources[0].text
    assert len(calls) == len(streams) == 1
    assert client.post(path + "/genera").json()["status"] == "completed"
    assert "Testo di fixture" not in capsys.readouterr().out


def test_invalid_citations_do_not_become_a_letter(
    web_client, monkeypatch, letter_factory, response_factory
):
    client, _, _ = web_client

    class Generator:
        usage = {}
        progress = {}

        def __init__(self, *args):
            pass

        async def stream(self, request):
            yield response_factory(letter_factory()).replace("evidenza-0001", "inventata")

    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    path = prepare(client)
    client.post(path + "/genera")
    result = wait_result(client, path)
    assert result["status"] == "failed" and "result" not in result
    assert client.get(path + "/fonti/source-0").status_code == 200
    assert client.post(path + "/genera").json()["status"] == "failed"


def test_restart_preserves_snapshot_and_marks_running_as_interrupted(tmp_path, web_client):
    client, _, _ = web_client
    path = prepare(client)
    directory = tmp_path / "web-sessions" / path.rsplit("/", 1)[1]
    state_file = directory / "state.json"
    state = json.loads(state_file.read_text())
    state["status"] = "running"
    state_file.write_text(json.dumps(state))
    app = create_app(output_dir=tmp_path)
    app.state.demo_identities = client.app.state.demo_identities
    with TestClient(app, headers={"Authorization": "Bearer demo-test"}) as restarted:
        assert restarted.get(path + "/stato").json()["status"] == "interrupted"
        assert restarted.get(path).json()["packet"]["context"]["id"] == "episode-x"


def test_second_preparation_cannot_start_while_another_generation_is_active(
    web_client, monkeypatch
):
    client, _, _ = web_client

    async def slow_run(**kwargs):
        await asyncio.sleep(0.3)
        raise RuntimeError("Errore simulato del serving")

    monkeypatch.setattr("codice_tesi.web_api.app.run_application", slow_run)
    first, second = prepare(client), prepare(client)
    assert client.post(first + "/genera").status_code == 202
    assert client.post(second + "/genera").status_code == 409
    assert client.get(second + "/stato").json()["status"] == "prepared"
    assert wait_result(client, first)["status"] == "failed"
