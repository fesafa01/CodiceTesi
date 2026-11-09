"""Citazioni riferite al catalogo reale, isolamento, lacune e preservazione dei fallimenti."""

import asyncio
import json
from hashlib import sha256

import pytest
from pydantic import ValidationError

from codice_tesi import application
from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.provenance import (
    EvidenceCatalog,
    TraceResult,
    build_catalog,
    parse_draft,
    render_draft,
    render_evidences,
    repair_mimic_references,
)
from codice_tesi.discharge.template import MISSING_VALUE


def test_mimic_repair_groups_known_evidences_and_preserves_the_section_text(packet_factory):
    catalog = build_catalog(packet_factory("prima", "seconda"))
    first, second = catalog.evidences[:2]
    citation = {"source_id": first.source_id, "evidence_ids": [first.id, second.id]}
    payload = {
        "sections": [
            {"section_id": "decorso", "text": "Testo di prova.", "sources": [citation, citation]},
            {"section_id": "istologico_in_attesa", "text": "No", "sources": []},
        ]
    }
    sections = json.loads(repair_mimic_references(json.dumps(payload), catalog))["sections"]
    assert sections[0]["text"] == "Testo di prova."
    assert sections[0]["sources"] == [
        {"source_id": item.source_id, "evidence_ids": [item.id]} for item in (first, second)
    ]
    assert sections[1]["text"] == MISSING_VALUE
    citation["evidence_ids"].append("ignota")
    assert repair_mimic_references(json.dumps(payload), catalog) is None


def test_catalog_preserves_original_unicode_and_makes_verified_identity_citable(packet_factory):
    text = "È vigile.\nNon riferisce dolore 👁.\n"
    packet = packet_factory("tipo_ignoto", text=text)
    catalog = build_catalog(packet)
    assert catalog.sources[0] == packet.sources[0]
    assert catalog.sources[0].text == text
    assert catalog.evidences[0].end == len(text)
    assert catalog.evidences[0].end != len(text.encode())
    context_source = catalog.sources[-1]
    assert context_source.context_id == packet.context.id
    assert "patient-x" in context_source.text and "episode-x" in context_source.text
    assert context_source.sha256 == sha256(context_source.text.encode()).hexdigest()
    assert build_catalog(packet) == catalog
    isolated = build_catalog(packet_factory("nota", isolated=True))
    assert "Identificativo paziente: non disponibile" in isolated.sources[-1].text


@pytest.mark.parametrize("fault", ["foreign", "hash", "offset", "source", "duplicate", "boolean"])
def test_catalog_rejects_invalid_evidence_and_foreign_sources(packet_factory, fault):
    data = build_catalog(packet_factory("nota")).model_dump(mode="json")
    if fault == "foreign":
        data["sources"][0]["context_id"] = "altro-ricovero"
    elif fault == "hash":
        data["sources"][0]["text"] = "Testo alterato"
    elif fault == "offset":
        data["evidences"][0]["end"] += 1
    elif fault == "source":
        data["evidences"][0]["source_id"] = "inesistente"
    elif fault == "duplicate":
        data["evidences"].append(data["evidences"][0])
    else:
        data["evidences"][0]["start"] = False
    with pytest.raises(ValidationError):
        EvidenceCatalog.model_validate(data)


def test_technical_source_id_cannot_collide_with_dataset_sources(packet_factory):
    data = packet_factory("nota").model_dump(mode="json")
    data["sources"][0]["id"] = "contesto-verificato-0001"
    catalog = build_catalog(CompositionPacket.model_validate(data))
    assert catalog.sources[-1].id == "contesto-verificato-0002"


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "evidence",
        "wrong_source",
        "duplicate_source",
        "duplicate_evidence",
        "missing_citation",
        "extra_key",
        "empty_required",
        "missing_section",
        "reordered",
        "duplicate_section",
        "invented_section",
        "empty_evidences",
        "invented_excerpt",
    ],
)
def test_untrusted_references_and_invalid_sections_are_rejected(
    packet_factory, letter_factory, response_factory, fault
):
    packet = packet_factory("prima", "seconda")
    data = json.loads(response_factory(letter_factory()))
    section = data["sections"][0]
    citation = section["sources"][0]
    if fault == "source":
        citation["source_id"] = "inesistente"
    elif fault == "evidence":
        citation["evidence_ids"] = ["inesistente"]
    elif fault == "wrong_source":
        citation["evidence_ids"] = ["evidenza-0002"]
    elif fault == "duplicate_source":
        section["sources"].append(citation.copy())
    elif fault == "duplicate_evidence":
        citation["evidence_ids"] *= 2
    elif fault == "missing_citation":
        section["sources"] = []
    elif fault == "extra_key":
        data["clinical_review"] = "passed"
    elif fault == "empty_required":
        section["text"], section["sources"] = "", []
    elif fault == "missing_section":
        data["sections"].pop()
    elif fault == "reordered":
        data["sections"].reverse()
    elif fault == "duplicate_section":
        data["sections"].append(section.copy())
    elif fault == "invented_section":
        section["section_id"] = "altra_sezione"
    elif fault == "empty_evidences":
        citation["evidence_ids"] = []
    else:
        citation["excerpt"] = "Estratto inventato dal modello."
    with pytest.raises(ValueError):
        parse_draft(json.dumps(data), build_catalog(packet), packet.template)


@pytest.mark.parametrize(
    "raw", ["{}", '{"sections":[', "```json\n{}\n```", '{"sections":[],"sections":[]}']
)
def test_invalid_or_ambiguous_json_is_not_repaired(packet_factory, raw):
    packet = packet_factory("nota")
    with pytest.raises(ValueError):
        parse_draft(raw, build_catalog(packet), packet.template)


@pytest.mark.parametrize("header", ["```", "```json", "```JSON"])
def test_single_json_fence_preserves_content_and_technical_checks(
    packet_factory, letter_factory, response_factory, header
):
    packet = packet_factory("nota")
    raw = response_factory(letter_factory())
    catalog = build_catalog(packet)
    expected = parse_draft(raw, catalog, packet.template)
    assert parse_draft(f"{header}\n{raw}\n```", catalog, packet.template) == expected
    bad = raw.replace("evidenza-0001", "evidenza-inesistente")
    with pytest.raises(ValueError, match="sconosciuta"):
        parse_draft(f"{header}\n{bad}\n```", catalog, packet.template)


@pytest.mark.parametrize("format", ["prose_before", "prose_after", "multiple", "unclosed"])
def test_fence_handling_does_not_extract_json_from_ambiguous_responses(
    packet_factory, letter_factory, response_factory, format
):
    packet = packet_factory("nota")
    raw = response_factory(letter_factory())
    fenced = f"```json\n{raw}\n```"
    candidate = {
        "prose_before": "Premessa\n" + fenced,
        "prose_after": fenced + "\nConclusione",
        "multiple": fenced + "\n" + fenced,
        "unclosed": "```json\n" + raw,
    }[format]
    with pytest.raises(ValueError):
        parse_draft(candidate, build_catalog(packet), packet.template)


def test_all_canonical_fields_explicitly_missing_are_preserved_without_citations(
    packet_factory, letter_factory, response_factory
):
    packet = packet_factory("nota")
    data = json.loads(response_factory(letter_factory()))
    section = next(s for s in data["sections"] if s["section_id"] == "medici")
    section.update(
        text="Specializzando: non disponibile.\nAltro medico: non disponibile.", sources=[]
    )
    draft = parse_draft(json.dumps(data), build_catalog(packet), packet.template)
    assert draft.sections[-1].text == section["text"]


@pytest.mark.parametrize(
    "text",
    [
        "Specializzando: Nome documentato.\nAltro medico: non disponibile.",
        "Specializzando: non presente.\nAltro medico: non disponibile.",
        "Specializzando: non disponibile.",
        "Specializzando: non disponibile.\nSpecializzando: non disponibile.",
        "Campo inventato: non disponibile.\nAltro medico: non disponibile.",
    ],
)
def test_missing_field_markers_do_not_allow_uncited_values_negations_or_incomplete_fields(
    packet_factory, letter_factory, response_factory, text
):
    packet = packet_factory("nota")
    data = json.loads(response_factory(letter_factory()))
    next(s for s in data["sections"] if s["section_id"] == "medici").update(text=text, sources=[])
    with pytest.raises(ValueError, match="senza citazioni"):
        parse_draft(json.dumps(data), build_catalog(packet), packet.template)


def test_missing_data_is_uncited_but_documented_no_requires_evidence(
    packet_factory, letter_factory, response_factory
):
    packet = packet_factory("nota")
    catalog = build_catalog(packet)
    data = json.loads(response_factory(letter_factory(answer="No")))
    data["sections"][0].update(text=MISSING_VALUE, sources=[])
    # Un campo facoltativo può rimanere vuoto senza citazione.
    data["sections"][2].update(text="", sources=[])
    parse_draft(json.dumps(data), catalog, packet.template)
    data["sections"][14]["sources"] = []
    with pytest.raises(ValueError, match="senza citazioni"):
        parse_draft(json.dumps(data), catalog, packet.template)
    data["sections"][14]["text"] = MISSING_VALUE
    parse_draft(json.dumps(data), catalog, packet.template)
    data["sections"][0]["sources"] = [{"source_id": "source-0", "evidence_ids": ["evidenza-0001"]}]
    with pytest.raises(ValueError, match="interamente indisponibile"):
        parse_draft(json.dumps(data), catalog, packet.template)


def test_view_uses_exact_catalog_slice_not_model_generated_quotes(
    packet_factory, letter_factory, response_factory
):
    text = "Prima riga.\nNon riferisce dolore 👁.\n```\nDopo."
    packet = packet_factory("nota", text=text)
    catalog_data = build_catalog(packet).model_dump(mode="json")
    catalog_data["evidences"][0].update(start=12, end=34)
    catalog = EvidenceCatalog.model_validate(catalog_data)
    raw = response_factory(letter_factory())
    draft = parse_draft(raw, catalog, packet.template)
    assert render_draft(draft, packet.template) == letter_factory()
    result = TraceResult(
        binding={
            "run_id": "fixture-run",
            "context_id": packet.context.id,
            "dataset": {"id": "fixture", "version": "1"},
            "template": {
                "id": packet.template.id,
                "version": packet.template.version,
                "sha256": packet.template.sha256,
            },
        },
        catalog=catalog,
        draft=draft,
        validation={"technical": "passed", "clinical_review": "not_performed"},
    )
    view = render_evidences(result, packet.template)
    assert text[12:34] in view and "Origine: fixture" in view
    assert "[12, 34)" in view and "correttezza clinica non verificate" in view
    data = result.model_dump(mode="json")
    data["binding"]["context_id"] = "altro-ricovero"
    with pytest.raises(ValidationError, match="fuori dal contesto"):
        TraceResult.model_validate(data)


def test_unknown_reference_preserves_raw_stream_and_does_not_publish_or_retry(
    packet_factory, letter_factory, response_factory, tmp_path, monkeypatch
):
    raw = response_factory(letter_factory()).replace("evidenza-0001", "inventata")
    calls = []

    async def fetch(self, **kwargs):
        return packet_factory("nota")

    class Generator:
        usage = {}

        def __init__(self, *args):
            pass

        async def stream(self, request):
            calls.append(request)
            yield raw

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    with pytest.raises(ValueError, match="evidenza sconosciuta"):
        asyncio.run(application.run_application(backend_url="http://test", output_dir=tmp_path))
    partial = next(tmp_path.rglob("risposta.parziale.json"))
    assert partial.read_text() == raw and len(calls) == 1
    assert (partial.parent / "catalogo_evidenze.json").exists()
    for name in ("bozza.txt", "evidenze.md", "bozza_strutturata.json"):
        assert not (partial.parent / name).exists()
    report = json.loads((partial.parent / "esito.json").read_text())
    assert report["stato"] == "fallito"
    assert report["tracciabilita"]["technical"] == "failed"
    assert report["tracciabilita"]["clinical_review"] == "not_performed"
    assert report["artefatti_sha256"][partial.name] == sha256(raw.encode()).hexdigest()
