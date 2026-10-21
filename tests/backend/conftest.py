"""Database PostgreSQL isolato per test; nessun dato clinico persistente modificato."""

import asyncio
import hashlib
import json
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from codice_tesi.backend.database import BackendSettings, Database, initialize_database


@pytest.fixture
def fhir(tmp_path):
    """Piccoli bundle FHIR di test, indipendenti dai pazienti del dataset locale."""

    def write(
        extra=(),
        *,
        main_updates=None,
        case_id="caso_01",
        patient_id="p",
        encounter_id="e",
        append=False,
    ):
        resources = [
            {"resourceType": "Patient", "id": patient_id, "gender": "female"},
            {
                "resourceType": "Encounter",
                "id": encounter_id,
                "status": "finished",
                "class": {"code": "IMP"},
                "subject": {"reference": f"Patient/{patient_id}"},
                "period": {"start": "2023-06-20T07:00:00Z", "end": "2023-06-23T12:00:00Z"},
                **(main_updates or {}),
            },
        ]
        raw = json.dumps(
            {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources + list(extra)]}
        ).encode()
        file = "patient.json" if case_id == "caso_01" else f"{case_id}.json"
        (tmp_path / file).write_bytes(raw)
        index = tmp_path / "indice.json"
        rows = json.loads(index.read_text()) if append and index.exists() else []
        rows.append(
            {
                "case_id": case_id,
                "patient_id": patient_id,
                "encounter_id": encounter_id,
                "fhir_file": file,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        index.write_text(json.dumps(rows))
        return tmp_path

    return write


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
