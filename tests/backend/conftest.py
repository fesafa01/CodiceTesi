"""Database PostgreSQL isolato per test; nessun dato clinico persistente modificato."""

import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from codice_tesi.backend.database import BackendSettings, Database, initialize_database


@pytest.fixture
def empty_settings():
    url = os.getenv("CLINICAL_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Impostare CLINICAL_TEST_POSTGRES_URL su un database PostgreSQL di test.")
    schema = f"test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    settings = BackendSettings(database_url=url, database_schema=schema, _env_file=None)

    async def prepare():
        async with Database(settings) as database:
            await initialize_database(database)

    try:
        asyncio.run(prepare())
        yield settings
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
