"""Pacchetto unico, isolamento delle note e adapter eCREAM."""

import json

import pytest
from pydantic import ValidationError

from codice_tesi.backend.importer import load_ecream
from codice_tesi.discharge.models import CompositionPacket, Context, EvidenceSource
from codice_tesi.discharge.template import load_template


def test_packet_contains_one_source_list_and_one_template(packet_factory):
    first = packet_factory("anamnesi")
    second = packet_factory("diario")
    assert first.template == second.template and len(first.template.sections) == 17
    data = first.model_dump(mode="json")
    assert set(data) == {"schema_version", "context", "sources", "template"}
    assert data["schema_version"] == "3.0" and len(data["sources"]) == 1


def test_source_kind_does_not_filter_notes(packet_factory):
    packet = packet_factory("unknown", "triage")
    assert [source.kind for source in packet.sources] == ["unknown", "triage"]


def test_episode_requires_verified_identity(packet_factory):
    data = packet_factory("triage", isolated=True).context.model_dump()
    with pytest.raises(ValidationError):
        Context.model_validate(dict(data, scope="episodio"))
    with pytest.raises(ValidationError):
        Context.model_validate(dict(data, patient_id="invented"))


def test_packet_rejects_changed_template_and_foreign_sources(packet_factory):
    data = packet_factory("anamnesi").model_dump(mode="json")
    data["sources"][0]["context_id"] = "other"
    with pytest.raises(ValidationError, match="fuori contesto"):
        CompositionPacket.model_validate(data)
    data = packet_factory("anamnesi").model_dump(mode="json")
    data["template"]["sections"][0]["title"] = "Titolo alterato"
    with pytest.raises(ValidationError, match="Template"):
        CompositionPacket.model_validate(data)


@pytest.mark.parametrize("invalid", ["empty", "duplicate", "unordered", "old_schema"])
def test_packet_rejects_invalid_source_lists_and_old_schema(packet_factory, invalid):
    data = packet_factory("anamnesi", "diario").model_dump(mode="json")
    if invalid == "empty":
        data["sources"] = []
    elif invalid == "duplicate":
        data["sources"].append(data["sources"][0])
    elif invalid == "unordered":
        data["sources"].reverse()
    else:
        data["schema_version"] = "2.0"
        data["sections"] = [{"section_id": "anamnesi", "sources": data.pop("sources")}]
    with pytest.raises(ValidationError):
        CompositionPacket.model_validate(data)


def test_source_hash_is_checked(packet_factory):
    data = packet_factory("anamnesi").sources[0].model_dump()
    with pytest.raises(ValidationError, match="Hash"):
        EvidenceSource.model_validate(dict(data, text="Testo modificato"))


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
    packet = CompositionPacket(
        context=grouped[0][0], sources=tuple(s for _, s in grouped), template=load_template()
    )
    assert len(packet.sources) == 2
    assert all("Testo C" not in s.text for s in packet.sources)
