"""Richieste e prenotazioni note alla dimissione, con esecuzione successiva."""

import json
from pathlib import Path
from runpy import run_path

import pytest

from codice_tesi.backend.synthea import load_synthea


def request(**fields):
    return {
        "resourceType": "ServiceRequest",
        "id": "control",
        "status": "active",
        "intent": "order",
        "subject": {"reference": "Patient/p"},
        "encounter": {"reference": "Encounter/e"},
        "authoredOn": "2023-06-23T12:00:00Z",
        "code": {"text": "Controllo cardiologico"},
        "occurrenceDateTime": "2023-07-07T09:00:00Z",
        **fields,
    }


def appointment(**fields):
    return {
        "resourceType": "Appointment",
        "id": "booking",
        "status": "booked",
        "created": "2023-06-23T12:00:00Z",
        "start": "2023-07-07T09:00:00Z",
        "end": "2023-07-07T09:30:00Z",
        "description": "Controllo prenotato",
        "participant": [{"actor": {"reference": "Patient/p"}, "status": "accepted"}],
        "extension": [
            {"url": "urn:codice-tesi:encounter", "valueReference": {"reference": "Encounter/e"}}
        ],
        **fields,
    }


def test_known_future_control_and_booking_survive_the_cutoff(fhir):
    root = fhir([request(), appointment()])
    _, loaded = load_synthea(root)
    text = "\n".join(s.text for _, s in loaded)
    assert "Controllo cardiologico" in text and "Controllo prenotato" in text
    assert "2023-07-07T09:00:00Z" in text
    assert "booked" in text and "ServiceRequest" in text


def test_later_creation_is_not_mistaken_for_a_known_discharge_plan(fhir):
    root = fhir(
        [
            request(authoredOn="2023-06-24T00:00:00Z"),
            appointment(created="2023-06-24T00:00:00Z"),
        ]
    )
    _, loaded = load_synthea(root)
    assert [s.kind for _, s in loaded] == ["contesto"]


def test_booked_patient_must_match_the_selected_episode(fhir):
    root = fhir(
        [appointment(participant=[{"actor": {"reference": "Patient/other"}, "status": "accepted"}])]
    )
    with pytest.raises(ValueError, match="altro paziente"):
        load_synthea(root)


def test_unlinked_appointment_does_not_expand_the_episode(fhir):
    root = fhir([appointment(extension=[])])
    _, loaded = load_synthea(root)
    assert len(loaded) == 1


def test_practitioner_participant_is_not_mistaken_for_a_second_patient(fhir):
    root = fhir(
        [
            {"resourceType": "Practitioner", "id": "doctor"},
            appointment(
                participant=[
                    {"actor": {"reference": "urn:uuid:p"}, "status": "accepted"},
                    {"actor": {"reference": "urn:uuid:doctor"}, "status": "accepted"},
                ]
            ),
        ]
    )
    _, loaded = load_synthea(root)
    assert "Controllo prenotato" in loaded[-1][1].text


def test_scenario_origin_is_explicit_and_unknown_enrichments_are_rejected(fhir):
    root = fhir()
    index = root / "indice.json"
    rows = json.loads(index.read_text())
    rows[0]["synthetic_enrichment"] = "dimissione"
    index.write_text(json.dumps(rows))
    digest, loaded = load_synthea(root)
    context = loaded[0][0]
    assert context.dataset == "synthea-dimissioni"
    assert "decisioni di uscita simulate" in context.limitations[0]
    rows[0]["synthetic_enrichment"] = "unknown"
    index.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="non supportata"):
        load_synthea(root)
    assert digest


def test_report_text_uses_result_availability_and_matches_export_precision():
    builder = run_path(str(Path(__file__).parents[2] / "scripts/build_discharge_dataset.py"))
    result = builder["available_at"](
        {
            "effectiveDateTime": "2023-06-22T08:00:00Z",
            "issued": "2023-06-24T10:00:00Z",
        }
    )
    assert result == "2023-06-24T10:00:00Z"
    # Regressione: 705 ms dopo il secondo di fine FHIR escludevano i nuovi ordini.
    assert builder["iso"](780461292705) == "1994-09-25T02:48:12+00:00"
