"""Configurazione, pool PostgreSQL e schema dei contesti documentali."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackendSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLINICAL_", env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql://clinico:clinico_sintetico@127.0.0.1:5433/clinico"
    )
    reference_reader_password: SecretStr | None = None
    database_schema: str = Field(default="public", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    pool_min_size: int = Field(default=1, ge=1, le=20)
    pool_max_size: int = Field(default=4, ge=1, le=20)
    pool_timeout: float = Field(default=5, gt=0, le=60)
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


class Database:
    def __init__(self, settings: BackendSettings):
        self.settings = settings
        self.pool = AsyncConnectionPool(
            conninfo=settings.database_url.get_secret_value(),
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            timeout=settings.pool_timeout,
            kwargs={"row_factory": dict_row},
            configure=self._configure,
            open=False,
        )

    async def _configure(self, connection: AsyncConnection):
        await connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(self.settings.database_schema))
        )
        await connection.execute("SET TIME ZONE 'UTC'")
        await connection.commit()

    async def open(self):
        await self.pool.open(wait=True, timeout=self.settings.pool_timeout)

    async def close(self):
        await self.pool.close()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    @asynccontextmanager
    async def connection(self, *, read_only: bool = False) -> AsyncIterator[AsyncConnection]:
        async with self.pool.connection() as connection, connection.transaction():
            if read_only:
                # Contesto e fonti della stessa lettura usano uno snapshot coerente.
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
            yield connection


async def initialize_database(database: Database) -> None:
    """Crea soltanto le tabelle mancanti nel database e schema configurati."""
    schema = Path(__file__).with_name("schema.sql").read_text()
    async with database.connection() as connection:
        await connection.execute(schema, prepare=False)
        password = database.settings.reference_reader_password
        if password is not None:
            await connection.execute("SELECT pg_advisory_xact_lock(73012026)")
            role = await connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'clinical_reference_reader'"
            )
            if await role.fetchone() is None:
                await connection.execute("CREATE ROLE clinical_reference_reader LOGIN")
            await connection.execute(
                sql.SQL("ALTER ROLE clinical_reference_reader PASSWORD {}").format(
                    sql.Literal(password.get_secret_value())
                )
            )
            schema_name = sql.Identifier(database.settings.database_schema)
            await connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO clinical_reference_reader").format(schema_name)
            )
            await connection.execute(
                sql.SQL(
                    "GRANT SELECT (id, payload) ON {}.composition_contexts "
                    "TO clinical_reference_reader"
                ).format(schema_name)
            )
            await connection.execute(
                sql.SQL(
                    "GRANT SELECT ON {}.mimic_discharge_references TO clinical_reference_reader"
                ).format(schema_name)
            )
