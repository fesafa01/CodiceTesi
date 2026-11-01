"""Tutti i campi e le note nel prompt, con controllo del budget prima dell'inferenza."""

import json

import pytest

from codice_tesi.config import ModelConfig
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.prompts import build_instructions, build_prompt
from codice_tesi.discharge.token_count import TOKENIZER_REVISION, estimate_input_tokens


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


def test_requirements_and_histology_empty_cell_rule_reach_model(packet_factory):
    text = build_instructions(packet_factory("diario").template)
    assert "11. Condizione clinica alla dimissione\nRequisito: obbligatorio." in text
    for title in (
        "3. Diagnosi alla dimissione testuale",
        "9. Esami di laboratorio",
        "12. Programma terapeutico, istruzioni e follow-up",
    ):
        assert title + "\nRequisito: facoltativo." in text
    assert "15. In attesa di esame istologico\nRequisito: obbligatorio." in text
    assert "Solo Sì oppure No, senza spiegazioni" in text
    assert "Con No lascia il contenuto vuoto, mantenendo l'intestazione" in text
    assert "segnala il dato mancante nel campo binario e lascia vuoti i dettagli" in text


def test_request_rejects_context_budget_before_inference_without_truncating_notes(packet_factory):
    packet = packet_factory("anamnesi", text="Fonte ampia. " * 10000)
    with pytest.raises(ValueError, match="Nessuna nota è stata troncata"):
        prepare_request(packet, ContextConfig(window_tokens=32768), output_tokens=8192)


def test_long_italian_notes_fit_with_tokenizer_and_are_preserved_in_full(packet_factory):
    text = "Paziente vigile, orientato. Non riferisce dolore. Temperatura 36,5 °C.\n" * 1600
    packet = packet_factory("diario", text=text)
    config, model = ContextConfig(), ModelConfig()
    request = prepare_request(packet, config, output_tokens=model.max_tokens, model_config=model)
    messages = [
        {"role": "system", "content": request.instructions},
        {"role": "user", "content": request.prompt},
    ]
    old_estimate = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 512
    assert old_estimate + model.max_tokens + config.safety_margin_tokens > config.window_tokens
    assert (
        request.input_tokens_estimate + model.max_tokens + config.safety_margin_tokens
        <= config.window_tokens
    )
    assert text in request.prompt
    assert request.input_estimate_method == "deepseek_v4_local_chat_template_v1"
    assert request.input_tokenizer_revision == TOKENIZER_REVISION


def test_budget_reserves_full_output_and_safety_margin_at_exact_boundary(packet_factory):
    packet = packet_factory("diario", text="Sintesi clinica con caratteri accentati: è vigile.")
    output, margin = 32768, 1024
    request = prepare_request(packet, ContextConfig(), output_tokens=output)
    required = request.input_tokens_estimate + output + margin
    assert (
        prepare_request(packet, ContextConfig(window_tokens=required), output_tokens=output)
        == request
    )
    with pytest.raises(ValueError, match="Nessuna nota è stata troncata"):
        prepare_request(packet, ContextConfig(window_tokens=required - 1), output_tokens=output)


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize(
    "thinking,effort,expected",
    [(False, "low", 25), (True, "low", 25), (True, "max", 104), (False, "max", 25)],
)
def test_official_encoding_reference_counts_include_chat_and_reasoning_prefixes(
    model, thinking, effort, expected
):
    # Conteggi di riferimento ottenuti con encoding_dsv4.py della revisione inclusa.
    estimate = estimate_input_tokens(
        "Sei un assistente.",
        "Febbre: 38,5 °C. Paziente vigile.",
        ModelConfig(model=model, thinking=thinking, reasoning_effort=effort),
    )
    assert estimate.tokens == expected
    assert estimate.tokenizer_revision == TOKENIZER_REVISION


def test_other_models_keep_explicit_conservative_fallback(packet_factory):
    request = prepare_request(
        packet_factory("diario"),
        ContextConfig(),
        output_tokens=512,
        model_config=ModelConfig(model="other-model"),
    )
    messages = [
        {"role": "system", "content": request.instructions},
        {"role": "user", "content": request.prompt},
    ]
    assert request.input_tokens_estimate == (
        len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 512
    )
    assert request.input_estimate_method == "utf8_json_bytes_plus_512_not_tokenizer_count"
    assert request.input_tokenizer_revision is None
