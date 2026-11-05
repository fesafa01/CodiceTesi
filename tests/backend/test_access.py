"""Identità fittizie: rifiuto delle credenziali e isolamento dei casi sintetici."""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from codice_tesi.access import LocalIdentity, local_identities
from codice_tesi.backend.app import create_app
from codice_tesi.backend.database import BackendSettings, Database
from codice_tesi.backend.repository import BackendError, read_packet, save_dataset

PREFIX = "/api/v1/compilazione"


@pytest.mark.parametrize(
    "path",
    ["/template", "/contesti", "/avvio-test?seed=42", "/contesti/case-01/pacchetto"],
)
@pytest.mark.parametrize("authorization", [None, "Basic demo", "Bearer errato", "Bearer"])
def test_credentials_required_before_reading_any_data(path, authorization, backend_app):
    # Nessun lifespan/DB: se la richiesta raggiungesse il repository fallirebbe.
    client = TestClient(backend_app(BackendSettings(_env_file=None)))
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.get(PREFIX + path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert response.json()["title"] == "AUTENTICAZIONE_RICHIESTA"
    assert "sources" not in response.json()
    assert "errato" not in response.text


@pytest.mark.parametrize("context_id", ["case-02", "inesistente"])
def test_out_of_scope_is_denied_before_database_access(context_id, caplog, backend_app):
    client = TestClient(backend_app(BackendSettings(_env_file=None)))
    with caplog.at_level(logging.INFO, logger="clinical_backend"):
        response = client.get(
            PREFIX + f"/contesti/{context_id}/pacchetto",
            headers={"Authorization": "Bearer test-medico-a"},
        )
    assert response.status_code == 403 and response.json()["title"] == "ACCESSO_NEGATO"
    assert context_id not in response.text
    assert "test-medico-a" not in caplog.text
    assert '"local_subject": "medico-a"' in caplog.text


def test_template_and_health_have_explicit_access_policy(backend_app, assigned_identities):
    app = backend_app(BackendSettings(_env_file=None))
    client = TestClient(app)
    assert client.get("/health/live").status_code == 200
    for identity in assigned_identities:
        response = client.get(
            PREFIX + "/template", headers={"Authorization": "Bearer " + identity.token}
        )
        assert response.status_code == 200 and len(response.json()["sections"]) == 17
    schema = app.openapi()
    assert schema["paths"][PREFIX + "/template"]["get"]["security"] == [{"IdentitaLocale": []}]
    assert "security" not in schema["paths"]["/health/live"]["get"]


def test_invalid_credential_does_not_leak_into_errors_or_logs(caplog, backend_app):
    client = TestClient(backend_app(BackendSettings(_env_file=None)))
    token = "PRIVATE_INVALID_TOKEN"
    with caplog.at_level(logging.INFO, logger="clinical_backend"):
        response = client.get(PREFIX + "/template", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert token not in response.text and token not in caplog.text


def test_identity_configuration_cannot_silently_disable_access_checks(assigned_identities):
    identity = assigned_identities[0]
    for identities in [(), (identity, identity), (LocalIdentity("x", "", frozenset()),)]:
        with pytest.raises(ValueError, match="identità locali"):
            create_app(BackendSettings(_env_file=None), identities=identities)


def test_repository_denies_explicit_and_empty_scope_without_connection():
    for kwargs in [
        {"context_id": "other", "allowed_context_ids": frozenset({"allowed"})},
        {"seed": 42, "allowed_context_ids": frozenset()},
    ]:
        with pytest.raises(BackendError) as error:
            asyncio.run(read_packet(None, **kwargs))
        assert error.value.status == 403


def test_cases_and_random_selection_respect_assigned_scope(
    empty_settings, episode_rows, backend_app
):
    rows = episode_rows(["Nota A"], ["Nota B"])

    async def prepare():
        async with Database(empty_settings) as database:
            await save_dataset(database, *rows)

    asyncio.run(prepare())
    with TestClient(backend_app(empty_settings)) as client:
        for identity, allowed, denied in [
            ("test-medico-a", "case-01", "case-02"),
            ("test-medico-b", "case-02", "case-01"),
        ]:
            headers = {"Authorization": "Bearer " + identity}
            response = client.get(PREFIX + f"/contesti/{allowed}/pacchetto", headers=headers)
            assert response.status_code == 200
            assert response.json()["context"]["id"] == allowed
            denied_response = client.get(PREFIX + f"/contesti/{denied}/pacchetto", headers=headers)
            assert denied_response.status_code == 403
            assert "sources" not in denied_response.json()
            for seed in [0, 1, 42, 99]:
                selected = client.get(PREFIX + f"/avvio-test?seed={seed}", headers=headers)
                assert selected.status_code == 200
                assert selected.json()["context"]["id"] == allowed
        for context_id in ["case-01", "case-02"]:
            assert (
                client.get(
                    PREFIX + f"/contesti/{context_id}/pacchetto",
                    headers={"Authorization": "Bearer test-revisore"},
                ).status_code
                == 200
            )


def test_random_selection_cannot_fall_back_to_someone_elses_case(
    empty_settings, episode_rows, backend_app
):
    rows = episode_rows(["Nota A"])  # Solo il caso 01, assegnato al medico A.

    async def prepare():
        async with Database(empty_settings) as database:
            await save_dataset(database, *rows)

    asyncio.run(prepare())
    with TestClient(backend_app(empty_settings)) as client:
        response = client.get(
            PREFIX + "/avvio-test?seed=42", headers={"Authorization": "Bearer test-medico-b"}
        )
        assert response.status_code == 403 and "sources" not in response.json()


def test_missing_mimic_configuration_grants_no_default_access(monkeypatch):
    monkeypatch.delenv("APP_MIMIC_TOKEN", raising=False)
    monkeypatch.delenv("APP_MIMIC_CONTEXTS", raising=False)
    assert local_identities() == ()
    client = TestClient(create_app(BackendSettings(_env_file=None)))
    response = client.get(PREFIX + "/template", headers={"Authorization": "Bearer demo-revisore"})
    assert response.status_code == 401
