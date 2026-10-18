"""Contratti FHIR e riferimenti su fixture temporanee senza dati clinici distribuiti."""

import base64
from decimal import Decimal

import pytest
from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.capabilitystatement import CapabilityStatement
from fhir.resources.R4B.operationoutcome import OperationOutcome

from codice_tesi.backend.fhir_adapter import RESOURCE_MODELS

R = "/api/v1/ricoveri/episode-a"
FHIR = f"{R}/fhir"


def get(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/fhir+json")
    return response.json()


def test_capabilities_declare_release_and_read_only_subset(client):
    result = get(client, f"{FHIR}/metadata")
    CapabilityStatement.model_validate(result)
    assert result["fhirVersion"] == "4.3.0"
    assert result["format"] == ["application/fhir+json", "application/json", "text/json"]
    assert "nessuna importazione" in result["description"]
    assert {r["type"] for r in result["rest"][0]["resource"]} == set(RESOURCE_MODELS)
    assert all(
        {i["code"] for i in r["interaction"]} == {"read", "search-type"}
        for r in result["rest"][0]["resource"]
    )


@pytest.mark.parametrize("path", ["metadata", "Patient", "Patient/patient-a"])
def test_format_override_is_applied_to_all_fhir_interactions(client, path):
    response = client.get(
        f"{FHIR}/{path}", params={"_format": "json"}, headers={"Accept": "application/fhir+xml"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/fhir+json"
    assert response.headers["vary"] == "Accept"
    rejected = client.get(f"{FHIR}/{path}", params={"_format": "xml"})
    assert rejected.status_code == 406
    assert rejected.json()["issue"][0]["code"] == "not-supported"


@pytest.mark.parametrize(
    ("accept", "status", "content_type"),
    [
        ("application/fhir+json; fhirVersion=4.3", 200, "application/fhir+json"),
        ('application/fhir+json; fhirVersion="4.3"', 200, "application/fhir+json"),
        ("application/json", 200, "application/json"),
        ("text/json", 200, "text/json"),
        ("application/*", 200, "application/fhir+json"),
        ("application/xml, application/json;q=0.5", 200, "application/json"),
        ("application/fhir+xml", 406, "application/fhir+json"),
        ("application/fhir+json; fhirVersion=4.0", 406, "application/fhir+json"),
        ("application/fhir+json; fhirVersion=5.0", 406, "application/fhir+json"),
        ("*/*;q=0", 406, "application/fhir+json"),
        ("application/*;q=0, text/*;q=0, */*;q=1", 406, "application/fhir+json"),
        ("application/fhir+json;q=NaN", 400, "application/fhir+json"),
    ],
)
def test_http_negotiates_supported_serialization_and_release(client, accept, status, content_type):
    response = client.get(f"{FHIR}/metadata", headers={"Accept": accept})
    assert response.status_code == status, response.text
    assert response.headers["content-type"].split(";")[0] == content_type
    if status != 200:
        OperationOutcome.model_validate(response.json())


def test_search_format_survives_pagination_and_ambiguous_override_is_rejected(client):
    response = client.get(f"{FHIR}/Observation", params={"_format": "json", "_count": 1})
    assert response.status_code == 200
    next_url = next(link["url"] for link in response.json()["link"] if link["relation"] == "next")
    assert "_format=json" in next_url
    assert client.get(next_url).status_code == 200
    assert client.get(f"{FHIR}/metadata?_format=json&_format=xml").status_code == 400


@pytest.mark.parametrize("resource_type", list(RESOURCE_MODELS))
def test_paged_resources_validate_and_can_be_read_in_the_same_context(client, resource_type):
    path = f"{FHIR}/{resource_type}?_count=2&ambito=paziente"
    ids = []
    while path:
        response = client.get(path)
        assert response.status_code == 200, response.text
        result = response.json()
        Bundle.model_validate(result)
        assert "dati" not in result
        for entry in result.get("entry", []):
            resource = entry["resource"]
            RESOURCE_MODELS[resource_type].model_validate(resource)
            assert resource["id"] not in ids
            ids.append(resource["id"])
            actual = get(client, entry["fullUrl"])
            assert actual == resource
        path = next((link["url"] for link in result["link"] if link["relation"] == "next"), None)
    assert ids


def test_patient_and_encounter_preserve_available_fields(client):
    patient = get(client, f"{FHIR}/Patient/patient-a")
    assert "gender" not in patient
    encounter = get(client, f"{FHIR}/Encounter/episode-a")
    assert encounter["status"] == "in-progress"
    assert "end" not in encounter["period"]
    assert get(client, f"{FHIR}/Encounter/visit-a")["class"]["code"] == "AMB"


def test_results_preserve_units_and_resolvable_source_links(client):
    report = get(client, f"{FHIR}/DiagnosticReport/report-a")
    assert len(report["result"]) == 2
    values = [get(client, f"{FHIR}/{ref['reference']}") for ref in report["result"]]
    assert Decimal(str(values[0]["valueQuantity"]["value"])) == Decimal("1")
    assert values[0]["valueQuantity"]["system"] == "http://unitsofmeasure.org"
    assert "coding" not in values[0]["code"]
    assert client.get(report["presentedForm"][0]["url"]).content.startswith(b"%PDF-")
    document = get(client, f"{FHIR}/DocumentReference/report-a")
    assert "hash" not in document["content"][0]["attachment"]
    assert "clinical-mock" not in str(document)


def test_native_note_and_other_patient_isolation(client):
    document = get(client, f"{FHIR}/DocumentReference/note-a")
    attachment = document["content"][0]["attachment"]
    assert "url" not in attachment
    assert base64.b64decode(attachment["data"]).decode() == "Test\nNota di test"
    for resource in ("Patient/patient-b", "Encounter/episode-b", "DocumentReference/note-b"):
        response = client.get(f"{FHIR}/{resource}")
        assert response.status_code == 404
        OperationOutcome.model_validate(response.json())


def test_fhir_preserves_visibility_and_channel_errors(client, control):
    control(scenario="updates")
    control(at="2000-01-02T11:00:00Z")
    assert get(client, f"{FHIR}/DocumentReference/note-a")["meta"]["versionId"] == "2"
    control(at="2000-01-02T12:00:00Z")
    assert client.get(f"{FHIR}/DiagnosticReport/report-a").status_code == 404
    control(scenario="offline")
    response = client.get(f"{FHIR}/DocumentReference", params={"canale": "cdr"})
    assert response.status_code == 503
    OperationOutcome.model_validate(response.json())
