"""Importazione atomica e pacchetti HTTP su PostgreSQL reale."""

import asyncio
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from codice_tesi.access import LocalIdentity
from codice_tesi.backend.app import create_app
from codice_tesi.backend.database import Database
from codice_tesi.backend.repository import save_dataset
from codice_tesi.discharge.models import Context, EvidenceSource


def test_empty_composition_database_has_actionable_error(empty_settings, backend_app):
    with TestClient(
        backend_app(empty_settings), headers={"Authorization": "Bearer test-revisore"}
    ) as client:
        assert client.get("/api/v1/compilazione/avvio-test?seed=42").status_code == 403
        assert client.get("/api/v1/compilazione/avvio-test?seed=-1").status_code == 422
        assert client.get("/api/v1/compilazione/contesti/case-30/pacchetto").status_code == 404
        template = client.get("/api/v1/compilazione/template")
        assert template.status_code == 200 and template.json()["version"] == "1.1.0"
        assert len(template.json()["sections"]) == 17


def test_import_and_http_isolation(empty_settings, episode_rows, backend_app):
    contexts, sources = episode_rows(["Nota A"], ["Nota B"])

    async def prepare():
        async with Database(empty_settings) as db:
            assert await save_dataset(db, contexts, sources) == (True, 2)
            assert await save_dataset(db, contexts, sources) == (False, 2)
            changed = [context.model_copy(update={"dataset_version": "2"}) for context in contexts]
            with pytest.raises(ValueError, match="differente"):
                await save_dataset(db, changed, sources)

    asyncio.run(prepare())
    with TestClient(
        backend_app(empty_settings), headers={"Authorization": "Bearer test-revisore"}
    ) as client:
        first = client.get("/api/v1/compilazione/avvio-test?seed=42").json()
        assert first == client.get("/api/v1/compilazione/avvio-test?seed=42").json()
        packet = client.get("/api/v1/compilazione/contesti/case-01/pacchetto").json()
        assert packet["context"]["patient_id"] == "patient-1"
        assert packet["context"]["episode_id"] == "episode-1"
        assert [s["id"] for s in packet["sources"]] == ["case-01-source-1"]
        assert client.post("/api/v1/compilazione/avvio-test?seed=42").status_code == 405


def test_repository_keeps_verified_episode_sources_ordered(empty_settings):
    context = Context(
        id="verified-x",
        dataset="fixture",
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
    identity = LocalIdentity("fixture", "demo-test", frozenset({"verified-x"}))
    with TestClient(
        create_app(empty_settings, identities=(identity,)),
        headers={"Authorization": "Bearer demo-test"},
    ) as client:
        response = client.get("/api/v1/compilazione/contesti/verified-x/pacchetto")
        assert response.status_code == 200
        packet = response.json()
        assert packet["context"]["patient_id"] == "patient-x"
        assert packet["schema_version"] == "3.0"
        assert [s["id"] for s in packet["sources"]] == ["source-0", "source-1"]
        assert packet["template"] == client.get("/api/v1/compilazione/template").json()


def test_case_contains_only_its_own_sources(empty_settings, episode_rows, backend_app):
    contexts, sources = episode_rows(["Nota A", "Nota B"], ["Nota C"])

    async def prepare():
        async with Database(empty_settings) as db:
            assert await save_dataset(db, contexts, sources) == (True, 2)
            assert await save_dataset(db, contexts, sources) == (False, 2)

    asyncio.run(prepare())
    with TestClient(
        backend_app(empty_settings), headers={"Authorization": "Bearer test-revisore"}
    ) as client:
        packet = client.get("/api/v1/compilazione/contesti/case-01/pacchetto").json()
        assert packet["context"]["scope"] == "episodio"
        assert set(packet) == {"schema_version", "context", "sources", "template"}
        notes = [s for s in packet["sources"] if s["kind"] == "nota"]
        assert len(notes) == 2
        assert {s["text"] for s in notes} == {"Nota A", "Nota B"}
        assert all("Nota C" not in s["text"] for s in packet["sources"])
        for source in packet["sources"]:
            assert json.dumps(packet).count(json.dumps(source["text"])) == 1
        other = client.get("/api/v1/compilazione/contesti/case-02/pacchetto").json()
        assert other["context"]["patient_id"] == "patient-2"
        notes = [s for s in other["sources"] if s["kind"] == "nota"]
        assert len(notes) == 1 and notes[0]["text"] == "Nota C"


def test_context_listing_comes_from_database_and_respects_assignment(
    empty_settings, episode_rows, backend_app
):
    contexts, sources = episode_rows(["Nota A"], ["Nota B"])

    async def prepare():
        async with Database(empty_settings) as db:
            await save_dataset(db, contexts, sources)

    asyncio.run(prepare())
    with TestClient(backend_app(empty_settings)) as client:
        for token, expected in [
            ("test-revisore", ["case-01", "case-02"]),
            ("test-medico-a", ["case-01"]),
            ("test-medico-b", ["case-02"]),
        ]:
            response = client.get(
                "/api/v1/compilazione/contesti", headers={"Authorization": "Bearer " + token}
            )
            assert response.status_code == 200
            assert [row["id"] for row in response.json()] == expected
            assert all("sources" not in row for row in response.json())
