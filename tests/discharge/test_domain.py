"""Isolamento del caso, distribuzione deterministica e adapter eCREAM."""

import json

import pytest
from pydantic import ValidationError

from codice_tesi.backend.datasets.ecream import load_ecream
from codice_tesi.discharge.models import CompositionPacket, Context, EvidenceSource
from codice_tesi.discharge.policy import prepare_packet, validate_plan


def test_template_is_independent_of_sources(packet_factory):
    first = packet_factory("anamnesi")
    second = packet_factory("diario")
    assert first.template == second.template and len(first.sections) == 17
    assert all(s.sources == first.sections[0].sources for s in first.sections)
    assert all(len(s.sources) == 1 for s in first.sections)
    with pytest.raises(ValueError, match="contesti diversi"):
        prepare_packet(
            first.context.model_copy(update={"id": "other"}), list(first.sections[5].sources)
        )


def test_source_kind_never_excludes_notes_from_any_field(packet_factory):
    packet = packet_factory("unknown")
    assert packet.excluded_source_ids == ()
    assert all(len(section.sources) == 1 for section in packet.sections)


def test_episode_requires_verified_identity(packet_factory):
    data = packet_factory(isolated=True).context.model_dump()
    with pytest.raises(ValidationError):
        Context.model_validate(dict(data, scope="episodio"))
    with pytest.raises(ValidationError):
        Context.model_validate(dict(data, patient_id="invented"))


def test_packet_rejects_changed_template_and_foreign_sources(packet_factory):
    data = packet_factory("anamnesi").model_dump(mode="json")
    data["sections"][5]["sources"][0]["context_id"] = "other"
    with pytest.raises(ValidationError, match="fuori contesto"):
        CompositionPacket.model_validate(data)
    data = packet_factory("anamnesi").model_dump(mode="json")
    data["template"]["sections"][0]["title"] = "Titolo alterato"
    with pytest.raises(ValueError, match="Template"):
        validate_plan(CompositionPacket.model_validate(data))


def test_policy_rejects_source_omitted_from_an_allowed_section(packet_factory):
    data = packet_factory("anamnesi").model_dump(mode="json")
    data["sections"][6]["sources"] = []
    with pytest.raises(ValueError, match="Piano"):
        validate_plan(CompositionPacket.model_validate(data))


def test_source_hash_is_checked(packet_factory):
    data = packet_factory("anamnesi").sections[5].sources[0].model_dump()
    with pytest.raises(ValidationError, match="Hash"):
        EvidenceSource.model_validate(dict(data, text="Testo modificato"))


def test_multiple_sources_have_stable_order(packet_factory):
    packet = packet_factory("anamnesi", "anamnesi")
    sources = list(packet.sections[5].sources)
    assert prepare_packet(packet.context, sources[::-1]) == prepare_packet(packet.context, sources)


def test_ecream_never_infers_patient_episode_or_uses_crf_annotations(tmp_path):
    path = tmp_path / "notes.jsonl"
    rows = [
        dict(
            note_id=i,
            split="test",
            text=f"---ANAMNESIS---\nNota {i}",
            annotations=[{"label_assigned": "DO_NOT_USE"}],
        )
        for i in (11, 12)
    ]
    path.write_text("\n".join(map(json.dumps, rows)))
    digest, loaded = load_ecream(path)
    assert len(loaded) == 2 and len(digest) == 64
    assert loaded[0][0].id != loaded[1][0].id
    assert all(context.patient_id is None and context.episode_id is None for context, _ in loaded)
    assert "DO_NOT_USE" not in loaded[0][1].model_dump_json()
    path.write_text("\n".join(map(json.dumps, rows + rows[:1])))
    _, repeated = load_ecream(path)
    assert len(repeated) == 2  # stesse note, annotazioni ripetute: nessun testo duplicato


def test_repeated_note_id_groups_distinct_notes_without_combining_other_ids(tmp_path):
    rows = [
        dict(note_id=11, text="---TRIAGE---\nTesto A", split="test"),
        dict(note_id=11, text="---ANAMNESIS---\nTesto B", split="test"),
        dict(note_id=12, text="---TRIAGE---\nTesto C", split="test"),
        dict(
            note_id=11, text="---TRIAGE---\nTesto A", split="test", annotations=[{"ignored": True}]
        ),
    ]
    path = tmp_path / "notes.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)))
    _, loaded = load_ecream(path)
    grouped = [(c, s) for c, s in loaded if c.id == "ecream-note-11"]
    assert len(loaded) == 3 and len(grouped) == 2
    assert len({s.id for _, s in loaded}) == 3
    assert all(
        c.scope == "gruppo_note" and c.patient_id is None and c.episode_id is None
        for c, _ in grouped
    )
    packet = prepare_packet(grouped[0][0], [s for _, s in grouped])
    assert all(len(section.sources) == 2 for section in packet.sections)
    assert all("Testo C" not in s.text for section in packet.sections for s in section.sources)
