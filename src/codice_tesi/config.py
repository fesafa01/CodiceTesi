"""Parametri in model.toml; connessione e override nel .env."""

from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        toml_file="model.toml",
        extra="forbid",
    )

    base_url: AnyHttpUrl = AnyHttpUrl("http://spark-413e.tail9c981a.ts.net:8000/v1")
    model: str = Field(default="deepseek-v4-flash", min_length=1)
    api_key: SecretStr = Field(default=SecretStr("not-required"), min_length=1)
    timeout: float = Field(default=120, gt=0)
    max_tokens: int = Field(default=2048, gt=0)
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, gt=0)
    min_p: float | None = Field(default=None, ge=0, le=1)
    stop: list[str] | None = None
    seed: int | None = None
    reasoning_effort: str | None = Field(default=None, min_length=1)
    tool_choice: Literal["auto", "none", "required"] | None = None
    stream: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    def request_settings(self) -> OpenAIChatModelSettings:
        """Traduce i nomi del server nei campi PydanticAI, omettendo quelli non impostati."""
        values = self.model_dump(
            include={"max_tokens", "temperature", "top_p", "seed", "tool_choice"},
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
