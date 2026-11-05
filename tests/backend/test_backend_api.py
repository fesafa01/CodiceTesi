"""Superficie del backend documentale e compatibilità degli errori HTTP."""

import pytest
from fastapi.testclient import TestClient

from codice_tesi.backend.app import create_app
from codice_tesi.backend.cli import main
from codice_tesi.backend.database import BackendSettings


def test_openapi_contains_only_composition_and_health_routes():
    app = create_app(BackendSettings(_env_file=None))
    assert set(app.openapi()["paths"]) == {
        "/health/live",
        "/health/ready",
        "/api/v1/compilazione/template",
        "/api/v1/compilazione/contesti",
        "/api/v1/compilazione/avvio-test",
        "/api/v1/compilazione/contesti/{context_id}/pacchetto",
    }


def test_template_endpoint_serves_updated_requirements_without_database(backend_app):
    # Questo endpoint usa la risorsa inclusa nel pacchetto, senza query SQL.
    client = TestClient(
        backend_app(BackendSettings(_env_file=None)),
        headers={"Authorization": "Bearer test-revisore"},
    )
    response = client.get("/api/v1/compilazione/template")
    assert response.status_code == 200 and response.json()["version"] == "1.1.0"
    sections = {section["id"]: section for section in response.json()["sections"]}
    assert sections["condizioni_dimissione"]["requirement"]["status"] == "required"
    for section_id in ("diagnosi_dimissione", "esami_laboratorio", "programma_followup"):
        assert sections[section_id]["requirement"]["status"] == "optional"
    assert sections["istologico_in_attesa"]["fields"][0]["type"] == "boolean"
    assert sections["informazioni_istologico"]["applies_when"]["equals"] is True


@pytest.mark.parametrize(
    "command", ["scenario", "import-dataset", "validate-dataset", "import-synthea"]
)
def test_removed_commands_are_rejected(command):
    with pytest.raises(SystemExit) as exc:
        main([command])
    assert exc.value.code == 2


def test_request_errors_keep_identifiers_and_do_not_echo_values(empty_settings, backend_app):
    with TestClient(
        backend_app(empty_settings), headers={"Authorization": "Bearer test-revisore"}
    ) as client:
        response = client.get(
            "/api/v1/compilazione/avvio-test?seed=private-test-value",
            headers={"X-Request-ID": "test-request-123"},
        )
        assert response.status_code == 422
        assert response.headers["content-type"] == "application/problem+json"
        assert response.headers["x-request-id"] == "test-request-123"
        assert response.json()["request_id"] == "test-request-123"
        assert "private-test-value" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/v1/ricoveri").status_code == 404
        assert client.get("/api/v1/ricoveri/absent/fhir/metadata").status_code == 404
