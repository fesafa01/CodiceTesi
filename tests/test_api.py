"""Verifica del protocollo HTTP e degli errori senza contattare il server reale."""

import os

import pytest
from pydantic import ValidationError

from codice_tesi.config import (
    DEEPSEEK_MODEL,
    GEMMA_MODEL,
    MISTRAL_MODEL,
    QWEN_MODEL,
    SPARK_OLLAMA_URL,
    ModelConfig,
    Settings,
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYDANTIC_AI_NO_BANNER", "1")


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


def test_model_selection_routes_to_ollama_or_existing_serving(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
    settings = Settings()
    assert settings.model == QWEN_MODEL
    assert str(settings.for_model(QWEN_MODEL).base_url) == SPARK_OLLAMA_URL
    monkeypatch.setenv("LLM_MODEL", GEMMA_MODEL)
    monkeypatch.setenv("LLM_OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    settings = Settings()
    assert settings.model == GEMMA_MODEL
    gemma = settings.for_model(settings.model)
    assert str(gemma.base_url) == "http://127.0.0.1:11434/v1"
    assert gemma.api_key.get_secret_value() == "ollama"
    assert str(settings.for_model(MISTRAL_MODEL).base_url) == "http://127.0.0.1:11434/v1"
    assert str(settings.for_model(DEEPSEEK_MODEL).base_url) == "http://127.0.0.1:8000/v1"
    monkeypatch.setenv("LLM_MODEL", DEEPSEEK_MODEL)
    assert Settings().model == DEEPSEEK_MODEL


@pytest.mark.parametrize("model", [GEMMA_MODEL, MISTRAL_MODEL])
def test_gemma_and_mistral_have_a_separate_timeout_setting(monkeypatch, model):
    monkeypatch.setenv("LLM_TIMEOUT", "750")
    monkeypatch.setenv("LLM_FIRST_TEXT_TIMEOUT", "700")
    monkeypatch.setenv("LLM_GEMMA_MISTRAL_TIMEOUT", "650")
    settings = Settings().for_model(model)
    assert settings.timeout == settings.first_text_timeout == 650
    qwen = Settings().for_model(QWEN_MODEL)
    assert qwen.timeout == 750
    assert qwen.first_text_timeout == 700


@pytest.mark.parametrize(("thinking", "effort"), [(True, "low"), (False, "none")])
@pytest.mark.parametrize("model", [QWEN_MODEL, GEMMA_MODEL, MISTRAL_MODEL])
def test_ollama_uses_reasoning_and_json_mode(model, thinking, effort):
    settings = ModelConfig(model=model, thinking=thinking).request_settings()
    expected = "high" if model == MISTRAL_MODEL and thinking else effort
    assert settings["openai_reasoning_effort"] == expected
    assert settings["extra_body"] == {"response_format": {"type": "json_object"}}


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
