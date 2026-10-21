"""Tutti i campi nelle istruzioni e tutte le note una sola volta nel prompt."""

import pytest

from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.prompts import build_instructions, build_prompt


def test_all_campus_sections_and_subfields_are_in_instructions_in_order(packet_factory):
    packet = packet_factory("triage")
    text = build_instructions(packet.template)
    positions = []
    for index, section in enumerate(packet.template.sections, 1):
        positions.append(text.index(f"{index}. {section.title}\n"))
        for field in section.fields:
            assert field.label in text
    assert positions == sorted(positions)
    assert "Non disponibile nelle note fornite." in text
    assert "CAMPO CORRENTE" not in text and "schema_output" not in text
    assert "patient_id" not in text and "episode_id" not in text


def test_all_notes_are_included_once(packet_factory):
    packet = packet_factory("anamnesi", "diario", isolated=True)
    prompt = build_prompt(packet)
    assert prompt.count("INIZIO NOTA source-0") == 1
    assert prompt.count("INIZIO NOTA source-1") == 1
    assert prompt.count("Dolore da tre giorni.") == 2
    assert "Ambito: nota_isolata" in prompt
    assert "Identificativo paziente: non disponibile" in prompt
    assert "BOZZA GIÀ SCRITTA" not in prompt


def test_request_rejects_context_budget_before_inference_without_truncating_notes(packet_factory):
    packet = packet_factory("anamnesi", text="Fonte ampia. " * 10000)
    with pytest.raises(ValueError, match="Nessuna nota è stata troncata"):
        prepare_request(packet, ContextConfig(window_tokens=32768), output_tokens=8192)
