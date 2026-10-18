"""Schema vuoto, importazione esplicita e transazioni PostgreSQL."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import ForeignKeyViolation, ReadOnlySqlTransaction

from codice_tesi.backend.app import create_app
from codice_tesi.backend.cli import main
from codice_tesi.backend.control import set_state
from codice_tesi.backend.database import Database, initialize_database
from codice_tesi.backend.importer import import_dataset
from codice_tesi.backend.repository import open_repository, snapshot


def test_empty_database_is_ready_without_clinical_data(empty_settings):
    with TestClient(create_app(empty_settings)) as client:
        assert client.get("/health/ready").status_code == 200
        for path in ("reparti", "ricoveri"):
            response = client.get(f"/api/v1/{path}")
            assert response.status_code == 200
            assert response.json()["dati"] == []
            meta = response.json()["meta"]
            assert meta["dataset_caricato"] is False
            assert meta["scenario"] is None and meta["versione_dataset"] is None
            assert meta["revisione"] == 0
            assert "x-simulation-run" not in response.headers
        assert client.get("/api/v1/ricoveri/absent").status_code == 404


def test_init_only_creates_schema_and_is_idempotent(empty_settings, monkeypatch, capsys):
    monkeypatch.setenv("CLINICAL_DATABASE_URL", empty_settings.database_url.get_secret_value())
    monkeypatch.setenv("CLINICAL_DATABASE_SCHEMA", empty_settings.database_schema)
    main(["init"])
    assert "Nessun dataset importato" in capsys.readouterr().out

    async def verify():
        async with Database(empty_settings) as db:
            await initialize_database(db)
            async with db.connection() as connection:
                assert (await snapshot(connection)).scenario is None
                for table in ("patients", "episodes", "clinical_sources", "scenarios"):
                    cursor = await connection.execute(f"SELECT count(*) AS n FROM {table}")
                    assert (await cursor.fetchone())["n"] == 0
                with pytest.raises(ValueError, match="Importare un dataset"):
                    await set_state(connection, scenario_id="absent")

    asyncio.run(verify())


def test_import_preserves_state_and_rejects_other_dataset(settings, dataset_dir, control):
    control(scenario="updates")

    async def verify():
        async with Database(settings) as db:
            async with db.connection() as connection:
                before = await snapshot(connection)
            assert await import_dataset(db, dataset_dir) is False
            async with db.connection() as connection:
                assert await snapshot(connection) == before
            path = dataset_dir / "manifest.json"
            data = json.loads(path.read_text())
            data["versione"] = "changed"
            path.write_text(json.dumps(data))
            with pytest.raises(ValueError, match="dataset diverso"):
                await import_dataset(db, dataset_dir)

    asyncio.run(verify())


def test_invalid_import_rolls_back(empty_settings, dataset_dir):
    path = dataset_dir / "patients.json"
    data = json.loads(path.read_text())
    data[1]["sintetico"] = False
    path.write_text(json.dumps(data))

    async def verify():
        async with Database(empty_settings) as db:
            with pytest.raises(ValueError, match="sintetico"):
                await import_dataset(db, dataset_dir)
            async with db.connection() as connection:
                for table in ("patients", "clinical_units", "scenarios"):
                    cursor = await connection.execute(f"SELECT count(*) AS n FROM {table}")
                    assert (await cursor.fetchone())["n"] == 0

    asyncio.run(verify())


def test_context_and_read_only_constraints(settings):
    async def verify():
        async with Database(settings) as db:
            with pytest.raises(ForeignKeyViolation):
                async with db.connection() as connection:
                    await connection.execute("""INSERT INTO clinical_sources
                        (id, patient_id, episode_id, category, kind, origin)
                        VALUES ('invalid', 'patient-b', 'episode-a',
                                'registrazione', 'test', 'test')""")
            with pytest.raises(ReadOnlySqlTransaction):
                async with open_repository(db) as repo:
                    await repo.connection.execute("DELETE FROM patients")

    asyncio.run(verify())
