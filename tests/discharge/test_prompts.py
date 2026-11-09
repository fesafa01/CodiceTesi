"""Tutti i campi e le note nel prompt, con controllo del budget prima dell'inferenza."""

import json
from hashlib import sha256

import pytest

from codice_tesi.config import ModelConfig
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.models import MIMIC_DATASET
from codice_tesi.discharge.prompts import build_instructions, build_prompt
from codice_tesi.discharge.provenance import parse_draft, render_draft
from codice_tesi.discharge.template import MISSING_VALUE
from codice_tesi.discharge.token_count import TOKENIZER_REVISION, estimate_input_tokens
from codice_tesi.discharge.validation import validate_draft


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


@pytest.mark.parametrize(
    "kind,origin,text,rule,documented,missing",
    [
        pytest.param(
            "contesto",
            "hosp/admissions + hosp/patients",
            "discharge_location=HOME",
            "riporta la destinazione e segnala",
            {
                "condizioni_dimissione": (
                    "Destinazione amministrativa a domicilio. "
                    "Stato clinico alla dimissione non documentato."
                ),
            },
            {"motivo_ricovero": MISSING_VALUE},
            id="solo-destinazione",
        ),
        pytest.param(
            "dati_ricovero",
            "hosp/prescriptions",
            "2141-01-02 10:00:00 | Ordine farmacologico in degenza | drug=Farmaco A",
            "gli ordini in degenza non attestano somministrazioni",
            {"decorso": "Il 02/01/2141 è documentato un ordine di Farmaco A in degenza."},
            {"terapia_dimissione": "", "programma_followup": ""},
            id="solo-ordine-in-degenza",
        ),
        pytest.param(
            "dati_ricovero",
            "hosp/pharmacy + hosp/prescriptions (Discharge Med)",
            "2141-01-02 | Ordine di farmacia per dimissione | drug=Farmaco A | "
            "dose_val_rx=10 | dose_unit_rx=mg",
            "nella terapia in dimissione come ordini documentati",
            {"terapia_dimissione": "Farmaco A, 10 mg."},
            {"programma_followup": ""},
            id="ordine-di-dimissione",
        ),
        pytest.param(
            "dati_ricovero",
            "hosp/poe + hosp/poe_detail",
            "After discharge: outpatient review requested in two weeks.",
            "In programma_followup, riporta solo un piano o un controllo esplicitamente",
            {"programma_followup": "Richiesto un controllo ambulatoriale tra due settimane."},
            {"terapia_dimissione": ""},
            id="controllo-dopo-la-dimissione",
        ),
        pytest.param(
            "dati_ricovero",
            "hosp/diagnoses_icd",
            "Diagnosi ICD codificata retrospettivamente | ICD-10: Z00 | seq_num=1",
            "La descrizione di un codice diagnostico non attesta",
            {"diagnosi_codificate": "ICD-10 Z00 (codifica amministrativa del ricovero)."},
            {"diagnosi_dimissione": ""},
            id="solo-codice-diagnostico",
        ),
        pytest.param(
            "dati_ricovero",
            "hosp/procedures_icd",
            "2141-01-02 | Procedura ICD codificata retrospettivamente | ICD-9: 4516 | seq_num=1",
            "Un codice procedurale, da solo, non attesta",
            {"procedure_codificate": "ICD-9 4516 (codifica amministrativa del ricovero)."},
            {"procedure_diagnostiche_terapeutiche": MISSING_VALUE},
            id="solo-codice-procedurale",
        ),
        pytest.param(
            "referto_radiologico",
            "note/radiology/referto-di-prova",
            "Biopsia sotto guida radiologica eseguita.",
            "Una biopsia senza esito documentato non dimostra che l'istologico sia in attesa",
            {"procedure_diagnostiche_terapeutiche": "Eseguita biopsia sotto guida radiologica."},
            {"istologico_in_attesa": MISSING_VALUE, "informazioni_istologico": ""},
            id="biopsia-senza-esito",
        ),
    ],
)
def test_mimic_prompt_and_expected_campus_draft(
    packet_factory, kind, origin, text, rule, documented, missing
):
    """Esercita il prompt e il contratto della risposta attesa senza inferenza."""
    base = packet_factory(kind, text=text)
    source = base.sources[0].model_copy(update={"origin": origin})
    packet = base.model_copy(
        update={
            "context": base.context.model_copy(update={"dataset": MIMIC_DATASET}),
            "sources": (source,),
        }
    )
    request = prepare_request(packet, ContextConfig(), output_tokens=512)
    baseline = prepare_request(base, ContextConfig(), output_tokens=512)
    assert request.prompt_version != baseline.prompt_version
    assert "# Guida alle fonti MIMIC-IV" not in baseline.instructions
    assert ("Le fonti icu/ descrivono" in request.instructions) == origin.startswith("icu/")
    assert rule in request.instructions
    assert "Scrivi una lettera italiana anche quando tutte le fonti sono inglesi." in (
        request.instructions
    )
    assert "Descrivi in frasi distinte i passaggi pertinenti attestati" in request.instructions
    assert f'Origine: "{origin}"' in request.prompt
    assert request.prompt.count(text) == 1
    assert request.instructions.count("ID canonico:") == len(packet.template.sections) == 17
    allowed = [
        json.loads(line.removeprefix("RIFERIMENTI AMMESSI "))
        for line in request.prompt.splitlines()
        if line.startswith("RIFERIMENTI AMMESSI ")
    ]
    assert {item["source_id"] for item in allowed} == {s.id for s in request.catalog.sources}
    assert {
        (item["source_id"], evidence_id) for item in allowed for evidence_id in item["evidence_ids"]
    } == {(e.source_id, e.id) for e in request.catalog.evidences}

    for section_id in documented.keys() & {"diagnosi_codificate", "procedure_codificate"}:
        guide = request.instructions.split(f"ID canonico: {section_id}\n")[1].split("\n\n")[0]
        assert f"Trascrivi i codici letterali da {origin}" in guide
        assert "associate alla dimissione" not in guide

    citation = {
        "source_id": source.id,
        "evidence_ids": [e.id for e in request.catalog.evidences if e.source_id == source.id],
    }
    sections = []
    for section in packet.template.sections:
        default = MISSING_VALUE if section.requirement.status in {"required", "unconfirmed"} else ""
        sections.append(
            {
                "section_id": section.id,
                "text": documented.get(section.id, missing.get(section.id, default)),
                "sources": [citation] if section.id in documented else [],
            }
        )
    expected = parse_draft(
        json.dumps({"sections": sections}), request.catalog, packet.template, dataset=MIMIC_DATASET
    )
    validate_draft(render_draft(expected, packet.template), packet.template)
    therapy = next(
        section for section in expected.sections if section.section_id == "terapia_dimissione"
    )
    assert therapy.text == documented.get("terapia_dimissione", "")
    (warning,) = [issue for issue in request.facts.issues if issue.section_id == therapy.section_id]
    assert warning.message not in request.prompt
    if therapy.text:
        assert warning.evidence_ids == tuple(citation["evidence_ids"])
        assert "via di somministrazione, frequenza, durata" in warning.message
        assert "dose" not in warning.message
    else:
        assert warning.evidence_ids == ()


def test_complete_discharge_order_has_no_review_warning(packet_factory):
    packet = packet_factory(
        "dati_ricovero",
        text=(
            "2141-01-02 | Ordine di farmacia per dimissione | drug=Farmaco A | "
            "dose_val_rx=10 | dose_unit_rx=mg | route=PO | doses_per_24_hrs=1 | "
            "duration=5 | duration_interval=days"
        ),
    )
    source = packet.sources[0].model_copy(
        update={"origin": "hosp/pharmacy + hosp/prescriptions (Discharge Med)"}
    )
    packet = packet.model_copy(
        update={
            "context": packet.context.model_copy(update={"dataset": MIMIC_DATASET}),
            "sources": (source,),
        }
    )
    request = prepare_request(packet, ContextConfig(), output_tokens=512)
    assert all(issue.section_id != "terapia_dimissione" for issue in request.facts.issues)


@pytest.mark.parametrize(
    "details, expected",
    [
        (None, "non risultano ordini strutturati"),
        (
            "field_name=plan | field_value=Post-discharge follow-up",
            "indicazioni dopo la dimissione e tempistica",
        ),
        (
            "field_name=plan | field_value=Outpatient follow-up after discharge",
            "Verificare tempistica",
        ),
        (
            "field_name=plan | field_value=After discharge, follow-up with cardiology in two weeks",
            None,
        ),
        (
            "field_name=plan | field_value=After discharge, follow-up with cardiology "
            "in two weeks. Appointment not booked.",
            None,
        ),
        (
            "field_name=plan | field_value=Outpatient follow-up after discharge | "
            "field_name=timing | field_value=Two weeks",
            None,
        ),
        (
            "field_name=plan | field_value=Outpatient follow-up after discharge | "
            "field_name=unrelated | field_value=2026-11-09",
            "Verificare tempistica",
        ),
        (
            "field_name=plan | field_value=No outpatient follow-up in two weeks after discharge",
            "indicazioni dopo la dimissione e tempistica",
        ),
    ],
)
def test_followup_review_warnings_are_separate_from_the_prompt(packet_factory, details, expected):
    packet = packet_factory(
        "dati_ricovero",
        text=f"2141-01-02 10:00:00 | Ordine POE | poe_id=ordine-1 | {details}"
        if details
        else "Nota",
    )
    source = packet.sources[0].model_copy(
        update={
            "origin": (
                "hosp/poe + hosp/poe_detail (candidati follow-up)" if details else "hosp/poe"
            )
        }
    )
    packet = packet.model_copy(
        update={
            "context": packet.context.model_copy(update={"dataset": MIMIC_DATASET}),
            "sources": (source,),
        }
    )
    request = prepare_request(
        packet, ContextConfig(), output_tokens=512, model_config=ModelConfig(tools_enabled=True)
    )
    assert "puoi usare leggi_evidenze" in request.instructions
    issues = [issue for issue in request.facts.issues if issue.section_id == "programma_followup"]
    if expected is None:
        assert issues == []
    else:
        (warning,) = issues
        assert expected in warning.message and warning.message not in request.prompt
        assert warning.evidence_ids == tuple(
            e.id for e in request.catalog.evidences if details and e.source_id == source.id
        )


def test_request_rejects_context_budget_before_inference_without_truncating_notes(packet_factory):
    packet = packet_factory("anamnesi", text="Fonte ampia. " * 10000)
    with pytest.raises(ValueError, match="Nessuna nota è stata troncata"):
        prepare_request(packet, ContextConfig(window_tokens=32768), output_tokens=8192)


@pytest.mark.parametrize("tools_enabled", [False, True])
def test_expanded_mimic_sends_full_catalog_above_previous_limit(packet_factory, tools_enabled):
    text = "\n".join(f"Evento di laboratorio numero {i} con valore e unità." for i in range(7500))
    packet = packet_factory("dati_ricovero", "dati_ricovero", text=text)
    therapy = "Ordine di dimissione sintetico da conservare."
    packet = packet.model_copy(
        update={
            "context": packet.context.model_copy(
                update={"dataset": MIMIC_DATASET, "expanded_sources": True}
            ),
            "sources": (
                packet.sources[0],
                packet.sources[1].model_copy(
                    update={
                        "text": therapy,
                        "origin": "hosp/pharmacy + hosp/prescriptions (Discharge Med)",
                        "sha256": sha256(therapy.encode()).hexdigest(),
                    }
                ),
            ),
        }
    )
    model = ModelConfig(tools_enabled=tools_enabled)
    result = prepare_request(
        packet, ContextConfig(), output_tokens=model.max_tokens, model_config=model
    )
    assert result.window_tokens is None
    assert result.input_tokens_estimate > 100000
    assert result.prompt.count("Evento di laboratorio numero ") == 7500
    assert result.prompt.count("PASSAGGIO evidenza-") == len(result.catalog.evidences)
    assert result.catalog.sources[0].text == text
    assert therapy in result.prompt
    assert ("Nei dati MIMIC gli aggregatori riconoscono" in result.instructions) == tools_enabled
    assert r"rappresenta gli a capo con \n" in result.instructions


def test_long_italian_notes_fit_with_tokenizer_and_are_preserved_in_full(packet_factory):
    text = "Paziente vigile, orientato. Non riferisce dolore. Temperatura 36,5 °C.\n" * 1600
    packet = packet_factory("diario", text=text)
    config, model = ContextConfig(window_tokens=100000), ModelConfig()
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
    boundary = prepare_request(packet, ContextConfig(window_tokens=required), output_tokens=output)
    assert boundary.prompt == request.prompt and boundary.instructions == request.instructions
    assert boundary.input_tokens_estimate == request.input_tokens_estimate
    assert boundary.window_tokens == required
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


def test_input_breakdown_counts_text_separately_and_is_not_claimed_for_other_models(packet_factory):
    packet = packet_factory("nota", text="Testo originale da conservare.")
    request = prepare_request(packet, ContextConfig(), output_tokens=100)
    counts = request.input_tokens_breakdown
    assert counts["source_text_tokens_sum"] > 0
    assert counts["data_prompt_tokens"] > counts["source_text_tokens_sum"]
    assert (
        sum(
            counts[k]
            for k in ("instruction_tokens", "data_prompt_tokens", "chat_template_tokens_approx")
        )
        == request.input_tokens_estimate
    )
    other = prepare_request(
        packet, ContextConfig(), output_tokens=100, model_config=ModelConfig(model="other-model")
    )
    assert other.input_tokens_breakdown == {}
