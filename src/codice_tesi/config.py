"""Parametri del modello in Python; credenziali e connessione nel .env."""

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelConfig(BaseModel):
    """Configurazione della redazione in streaming; max_tokens è il limite per l’intera lettera."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)

    model: str = Field(default="deepseek-v4-flash", min_length=1)
    max_tokens: int = Field(default=8192, gt=0)
    temperature: float | None = Field(default=0, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, gt=0)
    min_p: float | None = Field(default=None, ge=0, le=1)
    stop: list[str] | None = None
    seed: int | None = None
    reasoning_effort: str | None = Field(default="low", min_length=1)

    def request_settings(self) -> OpenAIChatModelSettings:
        """Traduce i nomi del server nei campi PydanticAI, omettendo quelli non impostati."""
        values = self.model_dump(
            include={"max_tokens", "temperature", "top_p", "seed"},
            exclude_none=True,
        )
        if self.stop is not None:
            values["stop_sequences"] = self.stop
        if self.reasoning_effort is not None:
            values["openai_reasoning_effort"] = self.reasoning_effort
        extra = self.model_dump(include={"top_k", "min_p"}, exclude_none=True)
        if extra:
            values["extra_body"] = extra
        return OpenAIChatModelSettings(**values)


class Settings(BaseSettings):
    """Connessione: argomenti Python, variabili d'ambiente, .env, default."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=".env", env_file_encoding="utf-8", extra="forbid"
    )

    base_url: AnyHttpUrl = AnyHttpUrl("http://spark-413e.tail9c981a.ts.net:8000/v1")
    api_key: SecretStr = Field(default=SecretStr("not-required"), min_length=1)
    timeout: float = Field(default=300, gt=0)
