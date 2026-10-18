"""Fixture minime e temporanee; nessun dataset clinico distribuito con la repo."""

import asyncio
import hashlib
import json
import os
from io import BytesIO
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from pypdf import PdfWriter

from codice_tesi.backend.app import create_app
from codice_tesi.backend.config import BackendSettings
from codice_tesi.backend.control import set_state
from codice_tesi.backend.database import Database, initialize_database
from codice_tesi.backend.dataset_types import aware
from codice_tesi.backend.importer import import_dataset


@pytest.fixture
def dataset_dir(tmp_path):
    directory = tmp_path / "dataset"
    directory.mkdir()

    def write(name, content):
        (directory / name).write_text(json.dumps(content), encoding="utf-8")

    writer, buffer = PdfWriter(), BytesIO()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    pdf = buffer.getvalue()
    (directory / "report.pdf").write_bytes(pdf)
    digest = hashlib.sha256(pdf).hexdigest()
    extraction = {
        "schema_version": "2.0",
        "fonte_id": "report-a",
        "versione": 1,
        "originale_sha256": digest,
        "sezioni": [{"id": "body", "titolo": "Test", "testo": "Documento di test", "pagina": 1}],
    }
    write("extract.json", extraction)

    def source(id_, patient="patient-a", episode="episode-a", *, document=False):
        row = {
            "schema_version": "2.0",
            "id": id_,
            "paziente_id": patient,
            "episodio_id": episode,
            "categoria": "documento" if document else "registrazione",
            "tipo": "referto_laboratorio" if document else "diario_clinico",
            "titolo": "Fonte di test",
            "sistema_origine": "test",
            "autore_id": None,
            "canali": ["cdr"] if document else ["sio"],
            "versione": 1,
            "stato": "finale",
            "data_clinica": "2000-01-02T09:00:00Z",
            "registrato_il": "2000-01-02T09:05:00Z",
            "disponibile_il": "2000-01-02T09:10:00Z",
            "sintetico": True,
        }
        if document:
            raw = (directory / "extract.json").read_bytes()
            row.update(
                originale={
                    "file": "report.pdf",
                    "media_type": "application/pdf",
                    "sha256": digest,
                    "size": len(pdf),
                },
                estrazione={
                    "file": "extract.json",
                    "media_type": "application/json",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                    "metodo": "generazione_sintetica",
                },
                risultati=[
                    {
                        "kind": "laboratorio",
                        "name": f"Misura {i}",
                        "value": i,
                        "unit": "g/L",
                        "unit_code": "g/L",
                        "clinical_at": "2000-01-02T09:00:00Z",
                    }
                    for i in (1, 2)
                ],
            )
        else:
            row["sezioni"] = [{"id": "body", "titolo": "Test", "testo": "Nota di test"}]
        return row

    sources = [
        source("note-a"),
        source("report-a", document=True),
        source("note-history", episode="visit-a"),
        source("note-b", "patient-b", "episode-b"),
    ]
    for row in sources:
        write(f"{row['id']}.json", row)
    write("sources.json", [{"id": r["id"], "file": f"{r['id']}.json"} for r in sources])
    revised = source("note-a")
    revised.update(
        versione=2, disponibile_il="2000-01-02T11:00:00Z", registrato_il="2000-01-02T10:50:00Z"
    )
    revised["sezioni"][0]["testo"] = "Nota corretta"
    write("note-update.json", revised)
    retired = source("report-a", document=True)
    retired.update(
        versione=2,
        stato="ritirato",
        estrazione=None,
        disponibile_il="2000-01-02T12:00:00Z",
        registrato_il="2000-01-02T11:50:00Z",
    )
    write("report-update.json", retired)
    write(
        "scenarios.json",
        [
            {"id": "baseline", "descrizione": "Test di base"},
            {
                "id": "updates",
                "descrizione": "Versioni",
                "fonti_aggiuntive": [
                    {"id": "note-a", "file": "note-update.json"},
                    {"id": "report-a", "file": "report-update.json"},
                ],
            },
            {
                "id": "offline",
                "descrizione": "Canale indisponibile",
                "canali_non_disponibili": ["cdr"],
            },
        ],
    )
    write(
        "units.json",
        {
            "unita_cliniche": [{"id": "unit-a", "nome": "Unità di test"}],
            "unita_degenza": [{"id": "ward-a", "nome": "Degenza di test"}],
        },
    )
    write(
        "patients.json",
        [
            {
                "id": f"patient-{i}",
                "nome": "Test",
                "cognome": i,
                "data_nascita": "1980-01-01",
                "sintetico": True,
            }
            for i in ("a", "b")
        ],
    )
    write(
        "episodes.json",
        [
            {
                "id": f"episode-{i}",
                "paziente_id": f"patient-{i}",
                "stato": "in_degenza",
                "ingresso_il": "2000-01-01T08:00:00Z",
                "nosologico": f"test-{i}",
                "unita_clinica_id": "unit-a",
                "unita_degenza_id": "ward-a",
                "letto": None,
                "dimissione_prevista_il": "2000-01-03",
                "dimissione_effettiva_il": None,
            }
            for i in ("a", "b")
        ],
    )
    write(
        "visits.json",
        [
            {
                "id": "visit-a",
                "paziente_id": "patient-a",
                "data": "1999-01-01T08:00:00Z",
                "descrizione": "Visita di test",
            }
        ],
    )
    write(
        "template.json",
        {
            "id": "template-a",
            "versione": 1,
            "unita_clinica_id": "unit-a",
            "campi": [
                {
                    "id": "text",
                    "etichetta": "Test",
                    "obbligatorio": None,
                    "attendibilita_obbligatorieta": "da_verificare",
                }
            ],
        },
    )
    write("coverage.json", [])
    write(
        "manifest.json",
        {
            "schema_version": "2.0",
            "versione": "test-only",
            "sintetico": True,
            "snapshot": "2000-01-02T10:00:00Z",
            "scenario_iniziale": "baseline",
            "descrizione": "Fixture temporanee",
            "files": dict(
                zip(
                    [
                        "reparti",
                        "pazienti",
                        "ricoveri",
                        "prestazioni",
                        "modello_lettera",
                        "fonti",
                        "scenari",
                        "copertura",
                    ],
                    [
                        "units.json",
                        "patients.json",
                        "episodes.json",
                        "visits.json",
                        "template.json",
                        "sources.json",
                        "scenarios.json",
                        "coverage.json",
                    ],
                    strict=True,
                )
            ),
        },
    )
    return directory


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


@pytest.fixture
def settings(empty_settings, dataset_dir):
    async def prepare():
        async with Database(empty_settings) as database:
            assert await import_dataset(database, dataset_dir)

    asyncio.run(prepare())
    return empty_settings


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def control(settings):
    def change(*, scenario=None, at=None):
        async def execute():
            async with Database(settings) as database, database.connection() as connection:
                return await set_state(
                    connection, scenario_id=scenario, at=aware(at) if at else None
                )

        return asyncio.run(execute())

    return change
