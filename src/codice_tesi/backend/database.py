"""Pool PostgreSQL asincrono e schema SQL esplicito, condivisi da API e futuri tool."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from codice_tesi.backend.config import BackendSettings


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
                # Stato, versioni e misure della stessa lettura usano uno snapshot coerente.
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
            yield connection


async def initialize_database(database: Database) -> None:
    """Crea soltanto le tabelle mancanti nel database e schema configurati."""
    schema = Path(__file__).with_name("schema.sql").read_text()
    async with database.connection() as connection:
        await connection.execute(schema, prepare=False)
