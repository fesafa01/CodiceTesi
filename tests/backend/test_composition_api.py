"""Importazione atomica e pacchetti HTTP su PostgreSQL reale."""

import asyncio
import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from codice_tesi.backend.app import create_app
from codice_tesi.backend.database import Database
from codice_tesi.backend.repository import save_dataset
from codice_tesi.backend.synthea import import_synthea
from codice_tesi.demo_access import DemoIdentity
from codice_tesi.discharge.models import Context, EvidenceSource


def test_empty_composition_database_has_actionable_error(empty_settings):
    with TestClient(
        create_app(empty_settings), headers={"Authorization": "Bearer demo-revisore"}
    ) as client:
        assert client.get("/api/v1/compilazione/avvio-test?seed=42").status_code == 403
        assert client.get("/api/v1/compilazione/avvio-test?seed=-1").status_code == 422
        assert (
            client.get("/api/v1/compilazione/contesti/synthea-caso-30/pacchetto").status_code == 404
        )
        template = client.get("/api/v1/compilazione/template")
        assert template.status_code == 200 and template.json()["version"] == "1.1.0"
        assert len(template.json()["sections"]) == 17


def test_synthea_import_and_http_isolation(empty_settings, fhir):
    path = fhir()
    fhir(case_id="caso_02", patient_id="p2", encounter_id="e2", append=True)

    async def prepare():
        async with Database(empty_settings) as db:
            assert await import_synthea(db, path) == (True, 2)
            assert await import_synthea(db, path) == (False, 2)
            index = path / "indice.json"
            index.write_text(index.read_text() + "\n")
            with pytest.raises(ValueError, match="differente"):
                await import_synthea(db, path)

    asyncio.run(prepare())
    with TestClient(
        create_app(empty_settings), headers={"Authorization": "Bearer demo-revisore"}
    ) as client:
        first = client.get("/api/v1/compilazione/avvio-test?seed=42").json()
        assert first == client.get("/api/v1/compilazione/avvio-test?seed=42").json()
        packet = client.get("/api/v1/compilazione/contesti/synthea-caso-01/pacchetto").json()
        assert packet["context"]["patient_id"] == "p"
        assert packet["context"]["episode_id"] == "e"
        assert [s["id"] for s in packet["sources"]] == ["synthea-caso-01-contesto"]
        assert client.post("/api/v1/compilazione/avvio-test?seed=42").status_code == 405


def test_repository_keeps_verified_episode_sources_ordered(empty_settings):
    context = Context(
        id="verified-x",
        dataset="synthea-dimissioni",
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
    identity = DemoIdentity("fixture", "demo-test", frozenset({"verified-x"}))
    with TestClient(
        create_app(empty_settings, demo_identities=(identity,)),
        headers={"Authorization": "Bearer demo-test"},
    ) as client:
        response = client.get("/api/v1/compilazione/contesti/verified-x/pacchetto")
        assert response.status_code == 200
        packet = response.json()
        assert packet["context"]["patient_id"] == "patient-x"
        assert packet["schema_version"] == "3.0"
        assert [s["id"] for s in packet["sources"]] == ["source-0", "source-1"]
        assert packet["template"] == client.get("/api/v1/compilazione/template").json()


def test_case_imports_all_notes_once_without_combining_other_patients(empty_settings, fhir):
    def note(id, text, patient="p", encounter="e"):
        return {
            "resourceType": "DocumentReference",
            "id": id,
            "subject": {"reference": f"Patient/{patient}"},
            "context": {"encounter": [{"reference": f"Encounter/{encounter}"}]},
            "date": "2023-06-21T00:00:00Z",
            "content": [
                {
                    "attachment": {
                        "contentType": "text/plain",
                        "data": base64.b64encode(text.encode()).decode(),
                    }
                }
            ],
        }

    first = note("note-a", "Nota A")
    duplicate = {
        "resourceType": "DiagnosticReport",
        "id": "report-a",
        "subject": {"reference": "Patient/p"},
        "encounter": {"reference": "Encounter/e"},
        "effectiveDateTime": "2023-06-21T00:00:00Z",
        "presentedForm": [first["content"][0]["attachment"]],
    }
    path = fhir([first, note("note-b", "Nota B"), duplicate])
    fhir(
        [note("note-c", "Nota C", "p2", "e2")],
        case_id="caso_02",
        patient_id="p2",
        encounter_id="e2",
        append=True,
    )

    async def prepare():
        async with Database(empty_settings) as db:
            assert await import_synthea(db, path) == (True, 2)
            assert await import_synthea(db, path) == (False, 2)

    asyncio.run(prepare())
    with TestClient(
        create_app(empty_settings), headers={"Authorization": "Bearer demo-revisore"}
    ) as client:
        packet = client.get("/api/v1/compilazione/contesti/synthea-caso-01/pacchetto").json()
        assert packet["context"]["scope"] == "episodio"
        assert set(packet) == {"schema_version", "context", "sources", "template"}
        notes = [s for s in packet["sources"] if s["kind"].startswith("nota-")]
        assert len(notes) == 2
        assert {s["text"].split("\n\n", 1)[1] for s in notes} == {"Nota A", "Nota B"}
        assert all("Nota C" not in s["text"] for s in packet["sources"])
        for source in packet["sources"]:
            assert json.dumps(packet).count(json.dumps(source["text"])) == 1
        other = client.get("/api/v1/compilazione/contesti/synthea-caso-02/pacchetto").json()
        assert other["context"]["patient_id"] == "p2"
        notes = [s for s in other["sources"] if s["kind"].startswith("nota-")]
        assert len(notes) == 1 and notes[0]["text"].endswith("\n\nNota C")
