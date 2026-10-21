"""Verifica del protocollo HTTP e degli errori senza contattare il server reale."""

import os
import sys

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from codice_tesi import api, main
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
    main.main(["modelli"])
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
    assert ModelConfig().temperature == 0.6
    assert ModelConfig().thinking is True


@pytest.mark.parametrize("overrides", [{"base_url": "invalid"}, {"timeout": 0}])
def test_invalid_connection_configuration(overrides):
    with pytest.raises(ValidationError):
        Settings.model_validate(overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_tokens": 0},
        {"first_text_timeout": 0},
        {"temperature": -0.1},
        {"top_p": 1.1},
        {"top_k": 0},
        {"min_p": -0.1},
        {"tool_choice": "required"},
        {"stream": True},
        {"temperatur": 0.2},
    ],
)
def test_invalid_configuration(overrides):
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(overrides)


def test_model_configuration_validates_python_assignments():
    config = ModelConfig(max_tokens=8192)
    config.temperature = 0.2
    assert config.request_settings().get("temperature") == 0.2
    with pytest.raises(ValidationError):
        config.max_tokens = 0
    assert config.max_tokens == 8192


def test_thinking_is_explicit_and_effort_only_applies_when_enabled():
    settings = ModelConfig(top_k=20, thinking=False).request_settings()
    assert settings["extra_body"] == {"top_k": 20, "thinking": {"type": "disabled"}}
    assert "openai_reasoning_effort" not in settings
    enabled = ModelConfig(thinking=True).request_settings()
    assert enabled["extra_body"] == {"thinking": {"type": "enabled"}}
    assert enabled["openai_reasoning_effort"] == "low"


def test_connection_failure_has_actionable_message(monkeypatch, capsys):
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    monkeypatch.setattr(api, "create_client", lambda settings: mock_client(handler))
    monkeypatch.setattr(sys, "argv", ["tesi-agent", "modelli"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1
    error = capsys.readouterr().err
    assert "Tailscale" in error
    assert "Traceback" not in error
