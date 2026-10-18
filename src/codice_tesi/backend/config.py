"""Configurazione del servizio clinico, separata dalle impostazioni del modello."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackendSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLINICAL_", env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql://clinico:clinico_sintetico@127.0.0.1:5433/clinico"
    )
    database_schema: str = Field(default="public", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    pool_min_size: int = Field(default=1, ge=1, le=20)
    pool_max_size: int = Field(default=4, ge=1, le=20)
    pool_timeout: float = Field(default=5, gt=0, le=60)
    dataset_dir: Path | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8001, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def postgresql_url(cls, value: SecretStr):
        if not value.get_secret_value().startswith(("postgresql://", "postgres://")):
            raise ValueError("Usare una stringa PostgreSQL nativa, senza driver SQLAlchemy.")
        return value

    @model_validator(mode="after")
    def pool_bounds(self):
        if self.pool_max_size < self.pool_min_size:
            raise ValueError("Il massimo del pool non può essere inferiore al minimo.")
        return self
