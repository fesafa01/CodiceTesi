"""Una chiamata HTTP, stream JSON progressivo, viste con citazioni e interruzioni."""

import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai.exceptions import ModelAPIError

from codice_tesi import api, application
from codice_tesi.config import DEEPSEEK_MODEL, GEMMA_MODEL, QWEN_MODEL, ModelConfig, Settings
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.generator import LLMDraftGenerator
from codice_tesi.discharge.limits import ModelInactivityError


def sse(text, finish=None):
    event = {
        "id": "stream-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }
    return f"data: {json.dumps(event)}\n\n".encode()


@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.parametrize("model", [DEEPSEEK_MODEL, QWEN_MODEL, GEMMA_MODEL])
def test_single_http_call_streams_before_completion_and_flushes_same_file(
    packet_factory, tmp_path, monkeypatch, capsys, thinking, model, letter_factory, response_factory
):
    packet = packet_factory("triage", "anamnesi")
    calls = []
    hidden = "Controllo interno sintetico."
    letter = letter_factory()
    response = response_factory(letter)

    async def fetch(self, **kwargs):
        return packet

    async def check():
        release = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                if thinking:
                    event = json.loads(sse("").decode().removeprefix("data: "))
                    event["choices"][0]["delta"] = {"reasoning_content": hidden}
                    yield f"data: {json.dumps(event)}\n\n".encode()
                yield sse(response[:30])
                await release.wait()
                yield sse(response[30:], "stop")
                yield b"data: [DONE]\n\n"

        def handler(request):
            calls.append(json.loads(request.content))
            body = calls[-1]
            assert len(calls) == 1 and request.url.path == "/v1/chat/completions"
            assert body["stream"] is True and not body.get("tools")
            assert body.get("tool_choice") in (None, "none")
            assert body["model"] == model and body["max_tokens"] == 8192
            if model in {QWEN_MODEL, GEMMA_MODEL}:
                assert body["response_format"] == {"type": "json_object"}
                assert body["reasoning_effort"] == ("low" if thinking else "none")
            else:
                assert "response_format" not in body
                assert body["top_k"] == 20
                assert body["thinking"] == {"type": "enabled" if thinking else "disabled"}
                assert body.get("reasoning_effort") == ("low" if thinking else None)
            assert [m["role"] for m in body["messages"]] == ["system", "user"]
            for i, field in enumerate(packet.template.sections, 1):
                assert f"{i}. {field.title}\n" in body["messages"][0]["content"]
            for source in packet.sources:
                assert body["messages"][1]["content"].count(f"INIZIO NOTA {source.id}") == 1
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=Stream()
            )

        def create_client(settings):
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
                config=ModelConfig(model=model, top_k=20, max_tokens=8192, thinking=thinking),
            )
        )
        try:
            async with asyncio.timeout(2):
                while True:
                    partials = list(tmp_path.rglob("risposta.parziale.json"))
                    if partials and partials[0].read_text() == response[:30]:
                        break
                    await asyncio.sleep(0.01)
            assert not task.done()
            assert response[:30] in capsys.readouterr().out
            release.set()
            directory = await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert (directory / "bozza.txt").read_text() == letter
        assert (directory / "risposta.parziale.json").read_text() == response
        report = json.loads((directory / "esito.json").read_text())
        assert report["richieste_llm_avviate"] == 1 and len(report["campi_previsti"]) == 17
        assert report["stato"] == "bozza_da_revisionare"
        assert report["richieste_llm_previste"] == 1
        assert report["config_modello"]["thinking"] is thinking
        assert report["consumo"]["stream"]["caratteri_ragionamento"] == (
            len(hidden) if thinking else 0
        )
        assert hidden not in "\n".join(p.read_text() for p in directory.iterdir())
        assert {p.name for p in directory.iterdir()} == {
            "richiesta.txt",
            "catalogo_evidenze.json",
            "fatti_documentali.json",
            "risposta.parziale.json",
            "bozza_strutturata.json",
            "evidenze.md",
            "bozza.txt",
            "esito.json",
        }
        structured = json.loads((directory / "bozza_strutturata.json").read_text())
        assert structured["binding"]["run_id"] == report["run_id"]
        assert structured["validation"] == {
            "technical": "passed",
            "clinical_review": "not_performed",
        }

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    asyncio.run(check())
    assert len(calls) == 1


def test_stream_has_finite_idle_timeout_without_retries():
    async def check():
        settings = Settings(timeout=15, _env_file=None)
        async with api.create_client(settings) as client:
            assert client.timeout == 15
            assert client.max_retries == 0

    asyncio.run(check())


@pytest.mark.parametrize("first_part", ["thinking", "whitespace", "text"])
def test_first_text_deadline_limits_silence_but_not_running_letter(packet_factory, first_part):
    async def check():
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                if first_part == "thinking":
                    event = json.loads(sse("").decode().removeprefix("data: "))
                    event["choices"][0]["delta"] = {"reasoning_content": "Controllo"}
                    yield f"data: {json.dumps(event)}\n\n".encode()
                else:
                    yield sse("Testo" if first_part == "text" else " \n")
                await asyncio.sleep(0.12)
                yield sse(" finale.", "stop")
                yield b"data: [DONE]\n\n"

        def handler(request):
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=Stream()
            )

        async with AsyncOpenAI(
            base_url="http://test/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            generator = LLMDraftGenerator(ModelConfig(first_text_timeout=0.06), client)
            request = prepare_request(packet_factory("diario"), ContextConfig(), output_tokens=100)

            async def consume():
                return "".join([chunk async for chunk in generator.stream(request)])

            if first_part == "text":
                assert await consume() == "Testo finale."
            else:
                with pytest.raises(ModelInactivityError, match="Limite di attesa") as error:
                    await consume()
                assert error.value.seconds == 0.06
                assert "primo_testo_secondi" not in generator.usage["stream"]

    asyncio.run(check())


def test_active_reasoning_renews_deadline_until_the_letter_starts(packet_factory):
    async def check():
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(6):
                    event = json.loads(sse("").decode().removeprefix("data: "))
                    event["choices"][0]["delta"] = {"reasoning_content": "Controllo"}
                    yield f"data: {json.dumps(event)}\n\n".encode()
                    await asyncio.sleep(0.03)
                yield sse("Lettera.", "stop")
                yield b"data: [DONE]\n\n"

        def handler(request):
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=Stream()
            )

        async with AsyncOpenAI(
            base_url="http://test/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            generator = LLMDraftGenerator(ModelConfig(first_text_timeout=0.1), client)
            request = prepare_request(packet_factory("diario"), ContextConfig(), output_tokens=100)
            assert "".join([chunk async for chunk in generator.stream(request)]) == "Lettera."
            assert generator.usage["stream"]["primo_testo_secondi"] >= 0.18

    asyncio.run(check())


@pytest.mark.parametrize("finish", ["stop", "length", "cancel"])
def test_thinking_is_counted_before_text_and_usage_survives_token_limit(packet_factory, finish):
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    hidden = "Ragionamento sintetico riservato al test."
    request = prepare_request(packet_factory("triage"), ContextConfig(), output_tokens=64)

    async def check():
        release = asyncio.Event()
        calls = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                event = json.loads(sse("").decode().removeprefix("data: "))
                event["choices"][0]["delta"] = {"reasoning_content": hidden}
                yield f"data: {json.dumps(event)}\n\n".encode()
                await release.wait()
                yield sse("Lettera." if finish == "stop" else "", finish)
                yield (
                    b'data: {"choices": [], "usage": {"prompt_tokens": 100, '
                    b'"completion_tokens": 64, "total_tokens": 164}}\n\n'
                )
                yield b"data: [DONE]\n\n"

        def handler(http_request):
            calls.append(json.loads(http_request.content))
            assert calls[0]["thinking"] == {"type": "enabled"}
            assert calls[0]["reasoning_effort"] == "low"
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=Stream()
            )

        async with AsyncOpenAI(
            base_url="http://test/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            generator = LLMDraftGenerator(ModelConfig(thinking=True, max_tokens=64), client)
            chunks = []

            async def consume():
                async for chunk in generator.stream(request):
                    chunks.append(chunk)

            task = asyncio.create_task(consume())
            try:
                async with asyncio.timeout(2):
                    while generator.progress.get("caratteri_ragionamento", 0) == 0:
                        await asyncio.sleep(0.01)
                assert generator.progress["fase"] == "ragionamento"
                assert generator.progress["caratteri_ragionamento"] == len(hidden)
                assert not chunks and not task.done()
                if finish == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert not chunks
                elif finish == "length":
                    release.set()
                    with pytest.raises(UnexpectedModelBehavior, match="token limit"):
                        await asyncio.wait_for(task, 2)
                    assert not chunks
                else:
                    release.set()
                    await asyncio.wait_for(task, 2)
                    assert "".join(chunks) == "Lettera."
                    assert "primo_testo_secondi" in generator.progress
                if finish != "cancel":
                    assert generator.usage["input_tokens"] == 100
                    assert generator.usage["output_tokens"] == 64
                    assert generator.usage["finish_reason"] == finish
                assert generator.usage["stream"]["caratteri_ragionamento"] == len(hidden)
                assert hidden not in json.dumps(generator.usage)
                assert len(calls) == 1
            finally:
                if not task.done():
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task

    asyncio.run(check())


def test_timeout_before_first_text_preserves_failure_without_retry(
    packet_factory, tmp_path, monkeypatch
):
    calls = []

    async def fetch(self, **kwargs):
        return packet_factory("triage")

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("No stream data", request=request)

    def create_client(settings):
        return AsyncOpenAI(
            base_url="http://test/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(api, "create_client", create_client)
    with pytest.raises(ModelAPIError, match="timed out"):
        asyncio.run(application.run_application(backend_url="http://test", output_dir=tmp_path))
    directory = next(tmp_path.rglob("risposta.parziale.json")).parent
    report = json.loads((directory / "esito.json").read_text())
    assert len(calls) == 1 and report["stato"] == "fallito"
    assert report["errore_tipo"] == "ModelAPIError"
    assert report["consumo"]["stream"]["caratteri_testo"] == 0
    assert not (directory / "bozza.txt").exists()


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_truncated_response_keeps_partial_and_does_not_retry(
    packet_factory, tmp_path, monkeypatch, finish, letter_factory, response_factory
):
    calls = []
    letter = letter_factory(answer="No")
    text = response_factory(letter) if finish == "stop" else "Testo"

    async def fetch(self, **kwargs):
        return packet_factory("triage")

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse(text, finish) + b"data: [DONE]\n\n",
        )

    def create_client(settings):
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
        assert (directory / "bozza.txt").read_text() == letter
    else:
        with pytest.raises(ValueError, match="parziale"):
            run()
        directory = next(tmp_path.rglob("risposta.parziale.json")).parent
        assert (directory / "risposta.parziale.json").read_text() == "Testo"
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
            assert (
                next(tmp_path.rglob("risposta.parziale.json")).read_text() == "Testo già scritto."
            )
            raise failure()

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    with pytest.raises(failure):
        asyncio.run(application.run_application(backend_url="http://test", output_dir=tmp_path))
    directory = next(tmp_path.rglob("risposta.parziale.json")).parent
    assert (directory / "risposta.parziale.json").read_text() == "Testo già scritto."
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
    assert {p.name for p in directory.iterdir()} == {
        "richiesta.txt",
        "catalogo_evidenze.json",
        "fatti_documentali.json",
        "esito.json",
    }
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
