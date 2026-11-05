"""Database PostgreSQL isolato per test; nessun dato clinico persistente modificato."""

import asyncio
import hashlib
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from codice_tesi.access import LocalIdentity
from codice_tesi.backend.app import create_app
from codice_tesi.backend.database import BackendSettings, Database, initialize_database
from codice_tesi.discharge.models import Context, EvidenceSource


@pytest.fixture
def assigned_identities():
    return (
        LocalIdentity("medico-a", "test-medico-a", frozenset({"case-01"})),
        LocalIdentity("medico-b", "test-medico-b", frozenset({"case-02"})),
        LocalIdentity("revisore", "test-revisore", frozenset({"case-01", "case-02", "case-30"})),
    )


@pytest.fixture
def backend_app(assigned_identities):
    return lambda settings: create_app(settings, identities=assigned_identities)


@pytest.fixture
def episode_rows():
    def build(*notes_by_episode):
        contexts, sources = [], []
        for number, notes in enumerate(notes_by_episode, 1):
            context = Context(
                id=f"case-{number:02}",
                dataset="fixture",
                dataset_version="1",
                scope="episodio",
                patient_id=f"patient-{number}",
                episode_id=f"episode-{number}",
            )
            contexts.append(context)
            for index, text in enumerate(notes, 1):
                sources.append(
                    EvidenceSource(
                        id=f"{context.id}-source-{index}",
                        context_id=context.id,
                        kind="nota",
                        text=text,
                        origin="fixture",
                        version="1",
                        sha256=hashlib.sha256(text.encode()).hexdigest(),
                    )
                )
        return contexts, sources

    return build


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
