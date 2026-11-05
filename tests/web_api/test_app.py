"""Snapshot, lavori idempotenti, citazioni e accessi senza serving LLM reale."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from codice_tesi import application
from codice_tesi.access import LocalIdentity
from codice_tesi.config import QWEN_MODEL
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
    monkeypatch.setenv("LLM_MODEL", QWEN_MODEL)
    app = create_app(output_dir=tmp_path, backend_url="http://test")
    app.state.identities = (
        LocalIdentity("test", "demo-test", frozenset({packet.context.id})),
        LocalIdentity("other", "demo-other", frozenset({"other"})),
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


@pytest.mark.parametrize(
    "options",
    [None]
    + [
        {"thinking": thinking, "tools_enabled": tools}
        for thinking in (False, True)
        for tools in (False, True)
    ],
)
def test_generation_uses_preloaded_packet_and_publishes_validated_result(
    web_client, monkeypatch, letter_factory, response_factory, capsys, options
):
    client, calls, packet = web_client
    streams = []
    configs = []

    class Generator:
        usage = {}
        progress = {}

        def __init__(self, config, *args):
            configs.append(config)

        async def stream(self, request):
            streams.append(request)
            await asyncio.sleep(0.05)
            yield response_factory(letter_factory())

    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    path = prepare(client)
    assert client.post(path + "/genera", json=options).status_code == 202
    assert client.post(path + "/genera").status_code == 202
    result = wait_result(client, path)
    assert result["status"] == "completed"
    assert result["result"]["binding"]["context_id"] == packet.context.id
    assert result["result"]["validation"]["technical"] == "passed"
    assert result["result"]["catalog"]["sources"][0]["text"] == packet.sources[0].text
    assert len(calls) == len(streams) == 1
    expected = options or {"thinking": True, "tools_enabled": False}
    assert configs[0].model == QWEN_MODEL
    assert configs[0].thinking is expected["thinking"]
    assert configs[0].tools_enabled is expected["tools_enabled"]
    assert configs[0].total_timeout is None
    assert result["options"] == {**expected, "timeout_seconds": None}
    assert client.post(path + "/genera").json()["status"] == "completed"
    assert "Testo di fixture" not in capsys.readouterr().out


@pytest.mark.parametrize("invalid_json", [False, True])
def test_invalid_draft_does_not_become_a_letter(
    web_client, monkeypatch, letter_factory, response_factory, invalid_json
):
    client, _, _ = web_client

    class Generator:
        usage = {}
        progress = {}

        def __init__(self, *args):
            pass

        async def stream(self, request):
            response = response_factory(letter_factory())
            if invalid_json:
                yield response.rstrip() + "}"
            else:
                yield response.replace("evidenza-0001", "inventata")

    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    path = prepare(client)
    client.post(path + "/genera")
    result = wait_result(client, path)
    assert result["status"] == "failed" and "result" not in result
    assert result["failure_code"] == (
        "invalid_draft_json" if invalid_json else "invalid_draft_contract"
    )
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
    app.state.identities = client.app.state.identities
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


def test_cancellation_is_authorized_idempotent_and_preserves_partial(
    web_client, monkeypatch, tmp_path
):
    client, _, _ = web_client
    closed = []

    class Generator:
        usage = {}
        progress = {"fase": "redazione"}

        def __init__(self, *args):
            pass

        async def stream(self, request):
            try:
                yield '{"sections":['
                await asyncio.sleep(10)
            finally:
                closed.append(True)

    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    path = prepare(client)
    client.post(path + "/genera")
    for _ in range(100):
        partials = list(tmp_path.rglob("risposta.parziale.json"))
        if partials and partials[0].read_text():
            break
        time.sleep(0.01)
    assert partials and partials[0].read_text() == '{"sections":['
    assert (
        client.post(
            path + "/interrompi", headers={"Authorization": "Bearer demo-other"}
        ).status_code
        == 403
    )
    response = client.post(path + "/interrompi")
    assert response.status_code == 200 and response.json()["status"] == "interrupted"
    assert client.post(path + "/interrompi").json()["status"] == "interrupted"
    assert closed and partials[0].read_text() == '{"sections":['
    assert not list(tmp_path.rglob("bozza.txt"))
    report = json.loads((partials[0].parent / "esito.json").read_text())
    assert report["stato"] == "interrotto"
    assert response.json()["run_id"] == report["run_id"]


@pytest.mark.parametrize("timeout_seconds", [60, 7200])
def test_options_progress_and_timeout_are_persisted_without_clinical_content(
    web_client, monkeypatch, timeout_seconds
):
    from codice_tesi.discharge.limits import ExecutionTimeoutError

    client, _, _ = web_client
    options = []

    async def run(**kwargs):
        options.append(kwargs["config"])
        kwargs["on_progress"](
            {"run_id": "test-run", "phase": "lettura_fonti", "progress": {"tool_calls": 1}}
        )
        await asyncio.sleep(0.1)
        raise ExecutionTimeoutError("scaduta")

    monkeypatch.setattr("codice_tesi.web_api.app.run_application", run)
    path = prepare(client)
    assert (
        client.post(
            path + "/genera", json={"tools_enabled": True, "timeout_seconds": timeout_seconds}
        ).status_code
        == 202
    )
    state = client.get(path + "/stato").json()
    assert state["run_id"] == "test-run" and state["phase"] == "lettura_fonti"
    assert state["progress"] == {"tool_calls": 1} and "packet" not in state
    result = wait_result(client, path)
    assert result["status"] == "failed" and result["failure_code"] == "total_timeout"
    assert options[0].tools_enabled and options[0].total_timeout == timeout_seconds
    assert result["options"]["timeout_seconds"] == timeout_seconds
    assert client.get(path).json()["facts"] == {
        "schema_version": "1.0",
        "extraction_version": "record-testuale-v1",
        "facts": [],
        "issues": [],
    }


def test_invalid_duration_does_not_start_a_job(web_client):
    client, _, _ = web_client
    path = prepare(client)
    assert client.post(path + "/genera", json={"timeout_seconds": 0}).status_code == 422
    assert client.get(path + "/stato").json()["status"] == "prepared"


def test_model_inactivity_is_reported_separately_from_generic_failure(web_client, monkeypatch):
    from codice_tesi.discharge.limits import ModelInactivityError

    client, _, _ = web_client

    async def run(**kwargs):
        raise ModelInactivityError(240)

    monkeypatch.setattr("codice_tesi.web_api.app.run_application", run)
    path = prepare(client)
    client.post(path + "/genera")
    result = wait_result(client, path)
    assert result["status"] == "failed"
    assert result["failure_code"] == "model_inactivity_timeout"
    assert "240 secondi" in result["detail"] and "in coda" in result["detail"]
    assert "generazioni già attive" in result["detail"] and "result" not in result


def test_manual_sections_are_saved_with_conflict_detection_and_survive_restart(
    web_client, tmp_path
):
    client, _, packet = web_client
    path = prepare(client)
    sid = packet.template.sections[0].id
    original = client.get(path).json()["packet"]
    body = {"revision": 0, "sections": {sid: "Testo manuale 👁\nDa conservare."}}
    saved = client.post(path + "/modifiche", json=body)
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    assert client.post(path + "/modifiche", json=body).status_code == 409
    assert client.get(path).json()["editor"]["sections"] == body["sections"]
    assert client.get(path).json()["packet"] == original
    app = create_app(output_dir=tmp_path)
    app.state.identities = client.app.state.identities
    with TestClient(app, headers={"Authorization": "Bearer demo-test"}) as restarted:
        assert restarted.get(path).json()["editor"] == saved.json()
        # Una stringa vuota è una modifica intenzionale; una chiave omessa la rimuove.
        empty = restarted.post(path + "/modifiche", json={"revision": 1, "sections": {sid: ""}})
        assert empty.json()["sections"] == {sid: ""}
        assert (
            restarted.post(path + "/modifiche", json={"revision": 2, "sections": {}}).json()[
                "sections"
            ]
            == {}
        )


def test_manual_sections_and_stream_share_preparation_access_controls(web_client):
    client, _, packet = web_client
    path = prepare(client)
    body = {"revision": 0, "sections": {packet.template.sections[0].id: "Testo"}}
    assert (
        client.post(
            path + "/modifiche", json=body, headers={"Authorization": "Bearer demo-other"}
        ).status_code
        == 403
    )
    assert (
        client.get(path + "/stream", headers={"Authorization": "Bearer demo-other"}).status_code
        == 403
    )
    assert (
        client.post(
            path + "/modifiche", json={"revision": 0, "sections": {"estranea": "Testo"}}
        ).status_code
        == 422
    )
    assert client.get(path).json()["editor"] == {"revision": 0, "sections": {}}


@pytest.mark.parametrize("tools", [False, True])
def test_live_sections_arrive_before_validation_without_overwriting_manual_text(
    web_client, monkeypatch, letter_factory, response_factory, tools
):
    client, _, packet = web_client
    raw = response_factory(letter_factory())
    cut = raw.index("Testo di fixture.") + len("Testo")

    class Generator:
        usage = {}
        progress = {"fase": "redazione"}

        def __init__(self, config, *args):
            self.tools = config.tools_enabled

        async def stream(self, request):
            for part in (raw[:cut], raw[cut:]):
                if self.tools:
                    self.partial_callback(part, False)
                else:
                    yield part
                await asyncio.sleep(0.4)
            if self.tools:
                yield raw

    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    path = prepare(client)
    sid = packet.template.sections[0].id
    client.post(path + "/genera", json={"tools_enabled": tools})
    saved = client.post(
        path + "/modifiche", json={"revision": 0, "sections": {sid: "Testo manuale"}}
    )
    assert saved.status_code == 200
    with client.stream("GET", path + "/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        states = [
            json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")
        ]
    partials = [
        s for s in states if s["status"] == "running" and s.get("preview", {}).get("sections")
    ]
    assert partials and "result" not in partials[0]
    assert partials[0]["preview"]["sections"][0] == {"section_id": sid, "text": "Testo"}
    assert states[-1]["status"] == "completed"
    assert states[-1]["result"]["draft"]["sections"][0]["text"] == "Testo di fixture."
    prepared = client.get(path).json()
    assert prepared["editor"]["sections"] == {sid: "Testo manuale"}
    assert prepared["result"] == states[-1]["result"]


def test_response_reset_and_failure_keep_only_the_last_partial(web_client, monkeypatch):
    client, _, packet = web_client
    sid = packet.template.sections[0].id

    async def run(**kwargs):
        kwargs["on_text"](f'{{"sections":[{{"section_id":"{sid}","text":"Vecchio', False)
        await asyncio.sleep(0.3)
        kwargs["on_text"]("", True)
        kwargs["on_text"](f'{{"sections":[{{"section_id":"{sid}","text":"Nuovo', False)
        raise ValueError("Interruzione del serving simulata")

    monkeypatch.setattr("codice_tesi.web_api.app.run_application", run)
    path = prepare(client)
    client.post(path + "/genera", json={"tools_enabled": True})
    result = wait_result(client, path)
    assert result["status"] == "failed" and "result" not in result
    assert result["preview"]["sections"] == [{"section_id": sid, "text": "Nuovo"}]
    assert result["preview"]["response_revision"] == 1


@pytest.mark.parametrize(
    "kind, code",
    [
        ("tool", "tool_call_invalid"),
        ("read", "tool_read_result_budget"),
        ("final", "tool_call_after_final"),
        ("protocol", "model_protocol_error"),
        ("usage", "model_usage_limit"),
    ],
)
def test_generation_failures_are_specific_without_exposing_model_content(
    web_client, monkeypatch, kind, code
):
    from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

    from codice_tesi.discharge.limits import (
        EvidenceReadError,
        FinalPhaseToolCallError,
        ToolCallValidationError,
    )

    client, _, _ = web_client

    async def run(**kwargs):
        if kind == "tool":
            raise ToolCallValidationError("leggi_evidenze")
        if kind == "read":
            raise EvidenceReadError("result_budget")
        if kind == "final":
            raise FinalPhaseToolCallError("leggi_evidenze")
        if kind == "protocol":
            raise UnexpectedModelBehavior("PRIVATE_MODEL_CONTENT")
        raise UsageLimitExceeded("PRIVATE_MODEL_CONTENT")

    monkeypatch.setattr("codice_tesi.web_api.app.run_application", run)
    path = prepare(client)
    client.post(path + "/genera", json={"thinking": False, "tools_enabled": True})
    result = wait_result(client, path)
    assert result["status"] == "failed" and result["failure_code"] == code
    assert "PRIVATE_MODEL_CONTENT" not in json.dumps(result)
    assert "Verificare il serving LLM" not in result["detail"]


def test_agent_api_import_does_not_load_sql_backend_or_driver():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import codice_tesi.web_api.app; "
                "assert not any(name.startswith(('codice_tesi.backend', 'psycopg')) "
                "for name in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
