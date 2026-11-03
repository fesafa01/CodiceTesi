"""Regressioni sui rami sì/no dell'istologico e conservazione degli output incoerenti."""

import asyncio
import json

import pytest

from codice_tesi import application
from codice_tesi.discharge.template import MISSING_VALUE, load_template
from codice_tesi.discharge.validation import validate_draft


@pytest.mark.parametrize(
    "answer,details",
    [
        ("No", ""),
        ("no", " \n\t"),
        ("Sì", "Esame della biopsia in attesa; ritiro ambulatoriale."),
        ("Sì", MISSING_VALUE),
        (MISSING_VALUE, ""),
    ],
)
def test_histology_accepts_binary_or_missing_with_corresponding_details(
    letter_factory, answer, details
):
    validate_draft(letter_factory(answer=answer, details=details), load_template())


@pytest.mark.parametrize(
    "answer,details",
    [
        ("No", "Non applicabile."),
        ("No", MISSING_VALUE),
        ("No", "-"),
        ("No", "Referto già disponibile."),
        ("Sì", ""),
        (MISSING_VALUE, "Biopsia in attesa."),
        ("", ""),
        ("Forse", ""),
        ("No, nessun esame in attesa.", ""),
    ],
)
def test_histology_rejects_nonbinary_or_inconsistent_details(letter_factory, answer, details):
    with pytest.raises(ValueError):
        validate_draft(letter_factory(answer=answer, details=details), load_template())


@pytest.mark.parametrize("change", ["missing", "duplicate", "reordered"])
def test_required_histology_headings_cannot_be_omitted_duplicated_or_reordered(
    letter_factory, change
):
    text = letter_factory(answer="No")
    first = "15. In attesa di esame istologico"
    second = "16. Informazioni sull’istologico"
    if change == "missing":
        text = text.replace(first, "")
    elif change == "duplicate":
        text = text.replace(first, first + "\n" + first)
    else:
        text = text.replace(first, "TEMP").replace(second, first).replace("TEMP", second)
    with pytest.raises(ValueError, match="intestazioni"):
        validate_draft(text, load_template())


@pytest.mark.parametrize("answer,details", [("No", MISSING_VALUE), ("Sì", "")])
def test_invalid_histology_preserves_partial_and_fails_application_without_retry(
    packet_factory, letter_factory, response_factory, tmp_path, monkeypatch, answer, details
):
    text = response_factory(letter_factory(answer=answer, details=details))
    calls = []

    async def fetch(self, **kwargs):
        return packet_factory("diario")

    class Generator:
        usage = {}

        def __init__(self, *args):
            pass

        async def stream(self, request):
            calls.append(request)
            yield text

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    with pytest.raises(ValueError):
        asyncio.run(application.run_application(backend_url="http://test", output_dir=tmp_path))
    partial = next(tmp_path.rglob("risposta.parziale.json"))
    assert partial.read_text() == text
    assert not (partial.parent / "bozza.txt").exists()
    report = json.loads((partial.parent / "esito.json").read_text())
    assert report["stato"] == "fallito" and report["errore_tipo"] == "ValueError"
    assert report["richieste_llm_avviate"] == 1 and len(calls) == 1
