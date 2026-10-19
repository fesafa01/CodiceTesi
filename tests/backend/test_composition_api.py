"""Importazione atomica e pacchetti HTTP su PostgreSQL reale."""

import asyncio
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from codice_tesi.backend.app import create_app
from codice_tesi.backend.composition_importer import import_ecream
from codice_tesi.backend.composition_repository import save_dataset
from codice_tesi.backend.database import Database
from codice_tesi.discharge.models import Context, EvidenceSource


def test_empty_composition_database_has_actionable_error(empty_settings):
    with TestClient(create_app(empty_settings)) as client:
        assert client.get("/api/v1/compilazione/avvio-test?seed=42").status_code == 409
        assert client.get("/api/v1/compilazione/avvio-test?seed=-1").status_code == 422
        assert client.get("/api/v1/compilazione/contesti/missing/pacchetto").status_code == 404
        template = client.get("/api/v1/compilazione/template")
        assert template.status_code == 200 and template.json()["version"] == "1.0.0"
        assert len(template.json()["sections"]) == 17


def test_ecream_import_and_http_isolation(empty_settings, tmp_path):
    path = tmp_path / "notes.jsonl"
    rows = [dict(note_id=i, split="test", text=f"---ANAMNESIS---\nTest {i}") for i in (1, 2)]
    path.write_text("\n".join(map(json.dumps, rows)))

    async def prepare():
        async with Database(empty_settings) as db:
            assert await import_ecream(db, path) == (True, 2)
            assert await import_ecream(db, path) == (False, 2)
            path.write_text(path.read_text() + "\n")
            with pytest.raises(ValueError, match="differente"):
                await import_ecream(db, path)

    asyncio.run(prepare())
    with TestClient(create_app(empty_settings)) as client:
        first = client.get("/api/v1/compilazione/avvio-test?seed=42").json()
        assert first == client.get("/api/v1/compilazione/avvio-test?seed=42").json()
        packet = client.get("/api/v1/compilazione/contesti/ecream-note-1/pacchetto").json()
        assert packet["context"]["patient_id"] is None
        assert [s["id"] for s in packet["sources"]] == ["ecream-1"]
        assert client.post("/api/v1/compilazione/avvio-test?seed=42").status_code == 405


def test_neutral_repository_supports_verified_episode_with_multiple_sources(empty_settings):
    context = Context(
        id="verified-x",
        dataset="other-dataset",
        dataset_version="v1",
        scope="episodio",
        patient_id="patient-x",
        episode_id="episode-x",
    )
    text = "Fonte di test"
    sources = [
        EvidenceSource(
            id=f"source-{i}",
            context_id=context.id,
            kind=kind,
            text=text,
            origin="fixture",
            version="1",
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
        for i, kind in enumerate(("anamnesi", "diario"))
    ]

    async def prepare():
        async with Database(empty_settings) as db:
            assert await save_dataset(db, [context], sources[::-1]) == (True, 1)
            assert await save_dataset(db, [context], sources) == (False, 1)

    asyncio.run(prepare())
    with TestClient(create_app(empty_settings)) as client:
        response = client.get("/api/v1/compilazione/contesti/verified-x/pacchetto")
        assert response.status_code == 200
        packet = response.json()
        assert packet["context"]["patient_id"] == "patient-x"
        assert packet["schema_version"] == "3.0"
        assert [s["id"] for s in packet["sources"]] == ["source-0", "source-1"]
        assert packet["template"] == client.get("/api/v1/compilazione/template").json()


def test_same_note_id_imports_one_context_with_all_distinct_texts(empty_settings, tmp_path):
    path = tmp_path / "grouped-notes.jsonl"
    first = dict(note_id=11, split="test", text="---TRIAGE---\nNota A")
    rows = [
        first,
        dict(note_id=11, split="test", text="---ANAMNESIS---\nNota B"),
        dict(note_id=12, split="test", text="---TRIAGE---\nNota C"),
        dict(first, annotations=[{"ignored": True}]),
    ]
    path.write_text("\n".join(map(json.dumps, rows)))

    async def prepare():
        async with Database(empty_settings) as db:
            assert await import_ecream(db, path) == (True, 2)
            assert await import_ecream(db, path) == (False, 2)

    asyncio.run(prepare())
    with TestClient(create_app(empty_settings)) as client:
        packet = client.get("/api/v1/compilazione/contesti/ecream-note-11/pacchetto").json()
        assert packet["context"]["scope"] == "gruppo_note"
        assert packet["context"]["patient_id"] is None
        assert set(packet) == {"schema_version", "context", "sources", "template"}
        assert len(packet["sources"]) == 2
        assert {s["text"] for s in packet["sources"]} == {first["text"], rows[1]["text"]}
        for source in packet["sources"]:
            assert json.dumps(packet).count(json.dumps(source["text"])) == 1
        other = client.get("/api/v1/compilazione/contesti/ecream-note-12/pacchetto").json()
        assert len(other["sources"]) == 1
        assert other["sources"][0]["text"] == rows[2]["text"]
