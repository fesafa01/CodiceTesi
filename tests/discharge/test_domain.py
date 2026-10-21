"""Pacchetto unico, identità del ricovero e isolamento delle fonti."""

import pytest
from pydantic import ValidationError

from codice_tesi.discharge.models import CompositionPacket, Context, EvidenceSource


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
