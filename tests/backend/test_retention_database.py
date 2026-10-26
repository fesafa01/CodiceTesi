"""Cancellazione SQL delle copie: schema isolato e caso di controllo preservato."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from codice_tesi.backend.database import BackendSettings, Database, initialize_database
from codice_tesi.backend.repository import BackendError, read_packet, save_dataset
from codice_tesi.backend.synthea import load_synthea
from codice_tesi.retention_demo import create_sandbox, purge_copies, register_database_copy


def test_expired_database_copy_is_removed_and_unregistered_control_survives(tmp_path, fhir):
    url = os.getenv("CLINICAL_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Impostare CLINICAL_TEST_POSTGRES_URL su PostgreSQL di test.")
    root = tmp_path / "retention"
    schema = create_sandbox(root)
    now = datetime(2026, 10, 26, 10, tzinfo=UTC)
    path = fhir()
    fhir(case_id="caso_02", patient_id="p2", encounter_id="e2", append=True)
    _, loaded = load_synthea(path)
    contexts = list({context.id: context for context, _ in loaded}.values())
    settings = BackendSettings(database_url=url, database_schema=schema, _env_file=None)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    async def verify():
        async with Database(settings) as database:
            await initialize_database(database)
            await save_dataset(database, contexts, [source for _, source in loaded])
            register_database_copy(root, "synthea-caso-01", created_at=now - timedelta(days=8))
            preview = await purge_copies(root, as_of=now)
            assert preview["planned_contexts"] == ["synthea-caso-01"]
            assert await read_packet(database, context_id="synthea-caso-01")
            result = await purge_copies(root, as_of=now, apply=True, database=database)
            assert result["deleted_contexts"] == 1 and result["deleted_sources"] >= 1
            with pytest.raises(BackendError) as error:
                await read_packet(database, context_id="synthea-caso-01")
            assert error.value.status == 404
            assert await read_packet(database, context_id="synthea-caso-02")
            repeated = await purge_copies(root, as_of=now, apply=True, database=database)
            assert not repeated["planned_contexts"] and repeated["deleted_sources"] == 0

    try:
        asyncio.run(verify())
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
