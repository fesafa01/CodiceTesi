"""Verifica del protocollo HTTP e degli errori senza contattare il server reale."""

import asyncio
import json
import os
import sys
from contextlib import aclosing

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError
from pydantic_ai.exceptions import UserError

from codice_tesi import api, cli
from codice_tesi.agent import create_agent, reply
from codice_tesi.config import ModelConfig, Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYDANTIC_AI_NO_BANNER", "1")


def mock_client(handler):
    return AsyncOpenAI(
        base_url="http://spark.test:8000/v1",
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {
            "temperature": 0.0,
            "top_p": 0.9,
            "top_k": 40,
            "min_p": 0.05,
            "stop": ["<END>"],
            "seed": 42,
            "reasoning_effort": "low",
        },
    ],
)
def test_agent_uses_configured_chat_endpoint(parameters):
    config = ModelConfig.model_validate(
        {"model": "deepseek-v4-pro", "max_tokens": 128, **parameters}
    )

    def handler(request):
        assert str(request.url) == "http://spark.test:8000/v1/chat/completions"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-v4-pro"
        assert body["max_tokens"] == 128
        assert "max_completion_tokens" not in body
        assert "extra_body" not in body
        for name, value in parameters.items():
            assert body[name] == value
        for name in ("temperature", "top_p", "top_k", "min_p", "stop", "seed", "reasoning_effort"):
            if name not in parameters:
                assert name not in body
        assert any("senza inventare" in message["content"] for message in body["messages"])
        assert body["messages"][-1]["content"] == "Rispondi OK"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": config.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21},
            },
        )

    async def check():
        client = mock_client(handler)
        async with create_agent(config, client=client) as agent:
            result = await agent.run("Rispondi OK")
            assert result.output == "OK"
        assert client.is_closed()

    asyncio.run(check())


def test_list_models(monkeypatch, capsys):
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "deepseek-v4-flash",
                        "object": "model",
                        "created": 1,
                        "owned_by": "local",
                    }
                ],
            },
        )

    monkeypatch.setattr(api, "create_client", lambda settings: mock_client(handler))
    asyncio.run(cli.run(None, list_models=True, settings=Settings(), config=ModelConfig()))
    assert capsys.readouterr().out == "deepseek-v4-flash\n"


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_TIMEOUT=30\nLLM_API_KEY=private-test-key\n")
    monkeypatch.setenv("LLM_TIMEOUT", "60")
    settings = Settings()
    assert settings.timeout == 60
    assert settings.api_key.get_secret_value() == "private-test-key"
    assert "private-test-key" not in repr(settings)


def test_model_configuration_is_only_from_python(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "environment-model")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.9")
    config = ModelConfig(model="deepseek-v4-pro", max_tokens=512, top_k=30, temperature=0.3)
    assert config.model == "deepseek-v4-pro"
    assert config.max_tokens == 512
    assert config.temperature == 0.3
    assert config.top_k == 30
    assert ModelConfig().model == "deepseek-v4-flash"
    assert ModelConfig().temperature is None


@pytest.mark.parametrize("overrides", [{"base_url": "invalid"}, {"timeout": 0}])
def test_invalid_connection_configuration(overrides):
    with pytest.raises(ValidationError):
        Settings.model_validate(overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_tokens": 0},
        {"temperature": -0.1},
        {"top_p": 1.1},
        {"top_k": 0},
        {"min_p": -0.1},
        {"tool_choice": "invalid"},
        {"temperatur": 0.2},
    ],
)
def test_invalid_configuration(overrides):
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(overrides)


def test_model_configuration_validates_python_assignments():
    config = ModelConfig()
    config.temperature = 0.2
    assert config.request_settings().get("temperature") == 0.2
    with pytest.raises(ValidationError):
        config.max_tokens = 0
    assert config.max_tokens == 2048


def test_connection_failure_has_actionable_message(monkeypatch, capsys):
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    monkeypatch.setattr(api, "create_client", lambda settings: mock_client(handler))
    monkeypatch.setattr(sys, "argv", ["tesi-agent", "--list-models"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    error = capsys.readouterr().err
    assert "Tailscale" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("stream", [True, False])
def test_cli_streams_by_default_and_can_disable_it(monkeypatch, capsys, stream):
    def handler(request):
        assert json.loads(request.content)["stream"] is stream
        if not stream:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-v4-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Ciao mondo"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        chunks = []
        for text, finish in (("Ciao ", None), ("mondo", "stop")):
            chunk = {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
            }
            chunks.append(f"data: {json.dumps(chunk)}\n\n")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content="".join(chunks) + "data: [DONE]\n\n",
        )

    monkeypatch.setattr(api, "create_client", lambda settings: mock_client(handler))
    monkeypatch.setattr(sys, "argv", ["tesi-agent", "Saluta"] + ([] if stream else ["--no-stream"]))
    cli.main()
    assert capsys.readouterr().out == "Ciao mondo\n"


def test_reply_yields_text_before_http_stream_finishes(monkeypatch):
    async def check():
        release = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for text, finish in (("Ciao ", None), ("mondo", "stop")):
                    chunk = {
                        "id": "chatcmpl-stream",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "deepseek-v4-flash",
                        "choices": [
                            {"index": 0, "delta": {"content": text}, "finish_reason": finish}
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n".encode()
                    if finish is None:
                        await release.wait()
                yield b"data: [DONE]\n\n"

        def handler(request):
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=Stream()
            )

        client = mock_client(handler)
        monkeypatch.setattr(api, "create_client", lambda settings: client)
        async with aclosing(reply("Saluta", ModelConfig())) as chunks:
            assert await asyncio.wait_for(anext(chunks), timeout=2) == "Ciao "
            release.set()
            assert "".join([text async for text in chunks]) == "mondo"
        assert client.is_closed()

    asyncio.run(check())


@pytest.mark.parametrize("tool_choice", ["auto", "none", "required"])
def test_registered_tools_and_tool_choice_are_sent(tool_choice):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["tool_choice"] == (tool_choice if len(requests) == 1 else "auto")
        assert body["tools"][0]["function"]["name"] == "echo"
        assert "strict" not in body["tools"][0]["function"]
        message = {"role": "assistant", "content": "OK"}
        finish = "stop"
        if tool_choice == "required" and len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-echo",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"text":"OK"}'},
                    }
                ],
            }
            finish = "tool_calls"
        elif tool_choice == "required":
            assert any(
                item["role"] == "tool" and item["content"] == "OK" for item in body["messages"]
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-tools",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish,
                    }
                ],
            },
        )

    async def check():
        async with create_agent(
            ModelConfig(tool_choice=tool_choice), client=mock_client(handler)
        ) as agent:

            @agent.tool_plain
            def echo(text: str) -> str:
                """Restituisce il testo ricevuto."""
                return text

            assert (await agent.run("Rispondi OK")).output == "OK"

    asyncio.run(check())
    assert len(requests) == (2 if tool_choice == "required" else 1)


def test_requiring_tools_without_registration_fails_before_request():
    def handler(request):
        pytest.fail("Non deve inviare richieste senza i tool richiesti")

    async def check():
        async with create_agent(
            ModelConfig(tool_choice="required"), client=mock_client(handler)
        ) as agent:
            with pytest.raises(UserError, match="no function tools"):
                await agent.run("Rispondi OK")

    asyncio.run(check())
