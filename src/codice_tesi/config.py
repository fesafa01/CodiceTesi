"""Parametri del modello in Python; credenziali e connessione nel .env."""

import os
from pathlib import Path

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_settings import BaseSettings, SettingsConfigDict

SPARK_LLM_URL = "http://spark-413e.tail9c981a.ts.net:8000/v1"
SPARK_OLLAMA_URL = "http://spark-413e.tail9c981a.ts.net:11434/v1"
DEEPSEEK_MODEL = "deepseek-v4-flash"
QWEN_MODEL = "qwen3.8:27b"
GEMMA_MODEL = "gemma4:31b"
OLLAMA_MODELS = frozenset({QWEN_MODEL, GEMMA_MODEL})


def default_output_dir() -> Path:
    """Restituisce il percorso degli artefatti dell'API web."""
    mimic = bool(os.getenv("APP_MIMIC_TOKEN"))
    if output_root := os.getenv("TESI_OUTPUT_DIR"):
        root = Path(output_root).expanduser()
    elif mimic:
        private = os.getenv("TESI_PRIVATE_DIR", "~/Desktop/codice-tesi-private")
        root = Path(private).expanduser() / "output"
    else:
        root = Path("output")
    return root / ("dimissioni-mimic" if mimic else "dimissioni")


class ModelConfig(BaseModel):
    """Redazione in streaming; max_tokens comprende anche l'eventuale ragionamento."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)

    model: str = Field(default=DEEPSEEK_MODEL, min_length=1)
    max_tokens: int = Field(default=32768, gt=0)
    temperature: float | None = Field(default=0.6, ge=0)
    top_p: float | None = Field(default=0.95, ge=0, le=1)
    top_k: int | None = Field(default=None, gt=0)
    min_p: float | None = Field(default=None, ge=0, le=1)
    stop: list[str] | None = None
    seed: int | None = None
    thinking: bool = True
    reasoning_effort: str | None = Field(default="low", min_length=1)
    # Prima del testo, ogni evento di reasoning rinnova questo limite di inattività.
    first_text_timeout: float = Field(default=240, gt=0)
    # Nessuna durata massima, salvo un limite richiesto esplicitamente.
    total_timeout: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    tools_enabled: bool = False
    max_requests: int = Field(default=4, ge=1, le=10)
    max_tool_calls: int = Field(default=6, ge=1, le=30)
    tool_validation_retries: int = Field(default=1, ge=0, le=1)
    max_tool_result_bytes: int = Field(default=24000, ge=1024, le=100000)
    total_tokens_limit: int = Field(default=120000, gt=0)

    def request_settings(self) -> OpenAIChatModelSettings:
        """Traduce i nomi del server nei campi PydanticAI, omettendo quelli non impostati."""
        values = self.model_dump(
            include={"max_tokens", "temperature", "top_p", "seed"},
            exclude_none=True,
        )
        if self.stop is not None:
            values["stop_sequences"] = self.stop
        if self.model in OLLAMA_MODELS:
            values["openai_reasoning_effort"] = (
                self.reasoning_effort or "medium" if self.thinking else "none"
            )
            values["extra_body"] = {"response_format": {"type": "json_object"}}
            return OpenAIChatModelSettings(**values)
        if self.thinking and self.reasoning_effort is not None:
            values["openai_reasoning_effort"] = self.reasoning_effort
        extra = self.model_dump(include={"top_k", "min_p"}, exclude_none=True)
        extra["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
        values["extra_body"] = extra
        return OpenAIChatModelSettings(**values)


class Settings(BaseSettings):
    """Connessione: argomenti Python, variabili d'ambiente, .env, default."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=".env", env_file_encoding="utf-8", extra="forbid"
    )

    model: str = Field(default=QWEN_MODEL, min_length=1)
    base_url: AnyHttpUrl = AnyHttpUrl(SPARK_LLM_URL)
    ollama_base_url: AnyHttpUrl = AnyHttpUrl(SPARK_OLLAMA_URL)
    api_key: SecretStr = Field(default=SecretStr("not-required"), min_length=1)
    timeout: float = Field(default=300, gt=0)

    def for_model(self, model: str) -> "Settings":
        if model in OLLAMA_MODELS:
            return self.model_copy(
                update={"base_url": self.ollama_base_url, "api_key": SecretStr("ollama")}
            )
        return self
