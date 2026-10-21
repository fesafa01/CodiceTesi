"""Schema documentale, transazioni atomiche e letture PostgreSQL."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import ReadOnlySqlTransaction, UniqueViolation

from codice_tesi.backend.app import create_app
from codice_tesi.backend.cli import main
from codice_tesi.backend.database import Database, initialize_database
from codice_tesi.backend.repository import read_packet, save_dataset
from codice_tesi.backend.synthea import import_synthea, load_synthea


def test_init_is_idempotent_and_creates_only_document_tables(empty_settings, monkeypatch, capsys):
    monkeypatch.setenv("CLINICAL_DATABASE_URL", empty_settings.database_url.get_secret_value())
    monkeypatch.setenv("CLINICAL_DATABASE_SCHEMA", empty_settings.database_schema)
    main(["init"])
    assert "Nessun dataset importato" in capsys.readouterr().out

    async def verify():
        async with Database(empty_settings) as db:
            await initialize_database(db)
            async with db.connection(read_only=True) as connection:
                cursor = await connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                )
                assert {r["tablename"] for r in await cursor.fetchall()} == {
                    "composition_contexts",
                    "composition_sources",
                }
                cursor = await connection.execute("SELECT count(*) AS n FROM composition_contexts")
                assert (await cursor.fetchone())["n"] == 0

    asyncio.run(verify())


def test_reinitialization_preserves_imported_data(empty_settings, fhir):
    path = fhir()

    async def verify():
        async with Database(empty_settings) as db:
            await import_synthea(db, path)
            before = await read_packet(db, context_id="synthea-caso-01")
            await initialize_database(db)
            assert await read_packet(db, context_id="synthea-caso-01") == before
            with pytest.raises(ReadOnlySqlTransaction):
                async with db.connection(read_only=True) as connection:
                    await connection.execute("DELETE FROM composition_sources")

    asyncio.run(verify())


def test_failed_source_insert_rolls_back_new_context(empty_settings, fhir):
    path = fhir()
    _, loaded = load_synthea(path)
    context, source = loaded[0]

    async def verify():
        async with Database(empty_settings) as db:
            await import_synthea(db, path)
            other = context.model_copy(update={"id": "other", "dataset": "other"})
            duplicate = source.model_copy(update={"context_id": other.id})
            with pytest.raises(UniqueViolation):
                await save_dataset(db, [other], [duplicate])
            async with db.connection(read_only=True) as connection:
                cursor = await connection.execute("SELECT id FROM composition_contexts")
                assert [r["id"] for r in await cursor.fetchall()] == [context.id]
            assert len((await read_packet(db, context_id=context.id)).sources) == 1

    asyncio.run(verify())


def test_readiness_checks_both_document_tables_even_when_empty(empty_settings):
    with TestClient(create_app(empty_settings)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200

        async def remove_sources():
            async with Database(empty_settings) as db, db.connection() as connection:
                await connection.execute("DROP TABLE composition_sources")

        asyncio.run(remove_sources())
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200
