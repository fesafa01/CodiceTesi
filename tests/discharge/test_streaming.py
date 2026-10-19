"""Una chiamata HTTP, streaming progressivo su terminale e TXT, interruzioni."""

import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI

from codice_tesi import api, application
from codice_tesi.config import ModelConfig, Settings
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request


def sse(text, finish=None):
    event = {
        "id": "stream-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }
    return f"data: {json.dumps(event)}\n\n".encode()


def test_single_http_call_streams_before_completion_and_flushes_same_file(
    packet_factory, tmp_path, monkeypatch, capsys
):
    packet = packet_factory("triage", "anamnesi")
    calls = []
    letter = "BOZZA DI LETTERA DI DIMISSIONE - DA REVISIONARE\n" + "\n".join(
        f"{index}. {field.title}\nTesto di fixture.\n"
        for index, field in enumerate(packet.template.sections, 1)
    )

    async def fetch(self, **kwargs):
        return packet

    async def check():
        release = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse(letter[:30])
                await release.wait()
                yield sse(letter[30:], "stop")
                yield b"data: [DONE]\n\n"

        def handler(request):
            calls.append(json.loads(request.content))
            body = calls[-1]
            assert len(calls) == 1 and request.url.path == "/v1/chat/completions"
            assert body["stream"] is True and not body.get("tools")
            assert body.get("tool_choice") in (None, "none")
            assert body["top_k"] == 20 and body["max_tokens"] == 8192
            assert [m["role"] for m in body["messages"]] == ["system", "user"]
            for i, field in enumerate(packet.template.sections, 1):
                assert f"{i}. {field.title}\n" in body["messages"][0]["content"]
            for source in packet.sources:
                assert body["messages"][1]["content"].count(f"INIZIO NOTA {source.id}") == 1
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=Stream()
            )

        def create_client(settings, *, streaming=False):
            assert streaming
            return AsyncOpenAI(
                base_url="http://test/v1",
                api_key="test",
                max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )

        monkeypatch.setattr(api, "create_client", create_client)
        task = asyncio.create_task(
            application.run_application(
                backend_url="http://test",
                output_dir=tmp_path,
                config=ModelConfig(top_k=20),
            )
        )
        try:
            async with asyncio.timeout(2):
                while True:
                    partials = list(tmp_path.rglob("bozza.parziale.txt"))
                    if partials and partials[0].read_text() == letter[:30]:
                        break
                    await asyncio.sleep(0.01)
            assert not task.done()
            assert letter[:30] in capsys.readouterr().out
            release.set()
            directory = await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert (directory / "bozza.txt").read_text() == letter
        assert not (directory / "bozza.parziale.txt").exists()
        report = json.loads((directory / "esito.json").read_text())
        assert report["richieste_llm_avviate"] == 1 and len(report["campi_previsti"]) == 17
        assert report["stato"] == "bozza_da_revisionare"

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    asyncio.run(check())
    assert len(calls) == 1


def test_stream_timeout_has_no_read_deadline_but_keeps_connection_timeout():
    async def check():
        settings = Settings(timeout=15, _env_file=None)
        async with api.create_client(settings, streaming=True) as client:
            assert client.timeout.read is None and client.timeout.connect == 15
            assert client.max_retries == 0
        async with api.create_client(settings) as client:
            assert client.timeout == 15

    asyncio.run(check())


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_truncated_response_keeps_partial_and_does_not_retry(
    packet_factory, tmp_path, monkeypatch, finish
):
    calls = []

    async def fetch(self, **kwargs):
        return packet_factory("triage")

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse("Testo", finish) + b"data: [DONE]\n\n",
        )

    def create_client(settings, *, streaming=False):
        return AsyncOpenAI(
            base_url="http://test/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(api, "create_client", create_client)

    def run():
        return asyncio.run(
            application.run_application(backend_url="http://test", output_dir=tmp_path)
        )

    if finish == "stop":
        directory = run()
        assert (directory / "bozza.txt").read_text() == "Testo"
    else:
        with pytest.raises(ValueError, match="parziale"):
            run()
        directory = next(tmp_path.rglob("bozza.parziale.txt")).parent
        assert (directory / "bozza.parziale.txt").read_text() == "Testo"
        assert not (directory / "bozza.txt").exists()
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
def test_error_or_cancellation_keeps_already_written_text(
    packet_factory, tmp_path, monkeypatch, failure
):
    async def fetch(self, **kwargs):
        return packet_factory("anamnesi")

    class Generator:
        usage = {}

        def __init__(self, *args):
            pass

        async def stream(self, request):
            yield "Testo già scritto."
            assert next(tmp_path.rglob("bozza.parziale.txt")).read_text() == "Testo già scritto."
            raise failure()

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    with pytest.raises(failure):
        asyncio.run(application.run_application(backend_url="http://test", output_dir=tmp_path))
    directory = next(tmp_path.rglob("bozza.parziale.txt")).parent
    assert (directory / "bozza.parziale.txt").read_text() == "Testo già scritto."
    assert not (directory / "bozza.txt").exists()
    report = json.loads((directory / "esito.json").read_text())
    assert report["stato"] == ("interrotto" if failure is asyncio.CancelledError else "fallito")


def test_whitespace_output_is_not_published(packet_factory, tmp_path, monkeypatch):
    async def fetch(self, **kwargs):
        return packet_factory("triage")

    class Generator:
        usage = {}

        def __init__(self, *args):
            pass

        async def stream(self, request):
            yield " \n"

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    with pytest.raises(ValueError, match="non ha prodotto testo"):
        asyncio.run(application.run_application(backend_url="http://test", output_dir=tmp_path))
    assert not list(tmp_path.rglob("bozza.txt"))


def test_dry_run_saves_one_exact_request_without_client_or_placeholders(
    packet_factory, tmp_path, monkeypatch
):
    packet = packet_factory("anamnesi", "diario")

    async def fetch(self, **kwargs):
        return packet

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(api, "create_client", lambda *a, **k: pytest.fail("LLM in dry-run"))
    directory = asyncio.run(
        application.run_application(backend_url="http://test", output_dir=tmp_path, dry_run=True)
    )
    assert directory.parent == tmp_path / packet.context.id
    assert {p.name for p in directory.iterdir()} == {"richiesta.txt", "esito.json"}
    text = (directory / "richiesta.txt").read_text()
    request = prepare_request(packet, ContextConfig(), output_tokens=8192)
    assert text == f"ISTRUZIONI\n\n{request.instructions}\n\nCONTESTO E NOTE\n\n{request.prompt}"
    report = json.loads((directory / "esito.json").read_text())
    assert report["richieste_llm_previste"] == 1 and report["richieste_llm_avviate"] == 0


def test_output_groups_runs_without_overwriting_and_handles_context_paths(
    packet_factory, tmp_path, monkeypatch
):
    from codice_tesi.discharge.models import CompositionPacket

    packet = packet_factory("anamnesi")

    async def fetch(self, **kwargs):
        return packet

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(api, "create_client", lambda *a, **k: pytest.fail("LLM in dry-run"))

    def run():
        return asyncio.run(
            application.run_application(
                backend_url="http://test", output_dir=tmp_path, dry_run=True
            )
        )

    first, second = run(), run()
    assert first != second and first.parent == second.parent == tmp_path / packet.context.id
    assert (first / "richiesta.txt").read_text() == (second / "richiesta.txt").read_text()
    parents = []
    for context_id in ("../../fuori", "..", "a/b", "a?b"):
        data = packet.model_dump(mode="json")
        data["context"]["id"] = context_id
        for source in data["sources"]:
            source["context_id"] = context_id
        packet = CompositionPacket.model_validate(data)
        directory = run()
        assert directory.parent.parent == tmp_path
        assert json.loads((directory / "esito.json").read_text())["contesto"]["id"] == context_id
        parents.append(directory.parent)
    assert len(set(parents)) == len(parents)


def test_oversized_notes_fail_before_creating_client(packet_factory, tmp_path, monkeypatch):
    async def fetch(self, **kwargs):
        return packet_factory("anamnesi", text="Nota ampia. " * 20000)

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(
        api, "create_client", lambda *a, **k: pytest.fail("LLM con budget superato")
    )
    with pytest.raises(ValueError, match="Nessuna nota è stata troncata"):
        asyncio.run(application.run_application(backend_url="http://test", output_dir=tmp_path))
    assert not list(tmp_path.rglob("bozza.txt"))


def test_default_cli_still_runs_document_workflow(monkeypatch):
    from codice_tesi import main

    calls = []

    async def run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(application, "run_application", run)
    main.main([])
    assert calls[0]["context_id"] is None and calls[0]["output_dir"] == Path("output/dimissioni")
