"""Contratti HTTP e isolamento delle fonti con fixture temporanee."""

import hashlib

from codice_tesi.backend.schemas import Problem

R = "/api/v1/ricoveri/episode-a"


def data(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()["dati"]


def test_patient_context_and_historical_sources(client):
    assert data(client, R)["paziente"]["id"] == "patient-a"
    assert len(data(client, f"{R}/fonti")) == 2
    assert len(data(client, f"{R}/fonti", ambito="paziente")) == 3
    assert client.get(f"{R}/fonti/note-b").status_code == 404
    assert client.get(f"{R}/documenti/note-a/contenuto").status_code == 404


def test_document_bytes_integrity_and_conditional_request(client):
    detail = data(client, f"{R}/fonti/report-a")
    response = client.get(detail["contenuto_url"])
    assert hashlib.sha256(response.content).hexdigest() == detail["sha256"]
    assert (
        client.get(
            detail["contenuto_url"], headers={"If-None-Match": response.headers["etag"]}
        ).status_code
        == 304
    )
    assert data(client, f"{R}/fonti/note-a")["contenuto_url"] is None
    assert len(data(client, f"{R}/risultati")) == 2


def test_pagination_is_exhaustive_and_bound_to_context(client, control):
    response = client.get(f"{R}/fonti", params={"limite": 1}).json()
    cursor = response["paginazione"]["prossimo_cursore"]
    assert cursor
    second = data(client, f"{R}/fonti", limite=1, cursore=cursor)
    assert second[0]["id"] != response["dati"][0]["id"]
    assert (
        client.get("/api/v1/ricoveri/episode-b/fonti", params={"cursore": cursor}).status_code
        == 409
    )
    control(at="2000-01-02T10:01:00Z")
    assert client.get(f"{R}/fonti", params={"cursore": cursor}).status_code == 409


def test_versions_withdrawals_and_channels(client, control):
    original = data(client, f"{R}/fonti/note-a")
    control(scenario="updates")
    assert data(client, f"{R}/fonti/note-a")["versione"] == 1
    control(at="2000-01-02T11:00:00Z")
    assert data(client, f"{R}/fonti/note-a")["versione"] == 2
    assert data(client, f"{R}/fonti/note-a", versione=1)["testo"] == original["testo"]
    control(at="2000-01-02T12:00:00Z")
    assert client.get(f"{R}/fonti/report-a").status_code == 404
    assert data(client, f"{R}/fonti/report-a", versione=1)["stato_corrente"] == "ritirato"
    control(scenario="offline")
    assert client.get(f"{R}/fonti", params={"canale": "cdr"}).status_code == 503
    assert client.get(f"{R}/fonti/note-b", params={"canale": "cdr"}).status_code == 404
    assert data(client, f"{R}/registrazioni")


def test_validation_read_only_and_error_privacy(client, caplog):
    response = client.get(
        f"{R}/fonti", params={"extra": "private-input"}, headers={"X-Request-ID": "request-test-01"}
    )
    assert response.status_code == 422
    Problem.model_validate(response.json())
    assert "private-input" not in response.text
    assert response.headers["x-request-id"] == "request-test-01"
    assert client.post(f"{R}/fonti", json={}).status_code == 405
    assert client.get(f"{R}/fonti", params={"limite": 101}).status_code == 422
    assert client.get(f"{R}/fonti", params={"cursore": "!!!"}).status_code == 400
    assert client.get(f"{R}/fonti", params={"dal": "2000-01-01T00:00:00"}).status_code == 422
    assert client.get(f"{R}/fonti", headers={"X-Clinical-Revision": "999"}).status_code == 409
