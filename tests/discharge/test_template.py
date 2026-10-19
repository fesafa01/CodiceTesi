"""Il template è l'unica definizione di struttura, regole e sottocampi."""

import pytest
from pydantic import ValidationError

from codice_tesi.discharge.template import LetterTemplate, load_template


def test_template_covers_all_campus_fields_and_preserves_uncertainty():
    template = load_template()
    assert template.id == "campus-discharge" and template.version == "1.0.0"
    assert len(template.sections) == 17 and len(template.sha256) == 64
    assert template.section("motivo_ricovero").requirement.status == "required"
    assert template.section("esami_laboratorio").requirement.status == "unconfirmed"
    assert template.section("condizioni_dimissione").requirement.status == "unconfirmed"
    assert template.section("medici").requirement.status == "optional"
    assert template.section("informazioni_istologico").requirement.status == "conditional"
    assert template.section("terapia_dimissione").repeatable
    assert [field.id for field in template.section("terapia_dimissione").fields] == [
        "farmaco",
        "dose",
        "via",
        "frequenza",
        "durata",
    ]
    assert all(section.requirement.basis and section.instruction for section in template.sections)
    assert any("scomposizione tecnica" in note for note in template.interpretation_notes)


def test_template_rejects_duplicate_fields_and_forward_conditions():
    data = load_template().model_dump(mode="json")
    data["sections"][0]["fields"].append(data["sections"][0]["fields"][0])
    with pytest.raises(ValidationError, match="duplicati"):
        LetterTemplate.model_validate(data)
    data = load_template().model_dump(mode="json")
    data["sections"][15]["applies_when"]["section_id"] = "medici"
    with pytest.raises(ValidationError, match="precedente"):
        LetterTemplate.model_validate(data)


def test_content_hash_changes_even_without_version_bump():
    original = load_template()
    data = original.model_dump(mode="json")
    data["sections"][0]["title"] = "Titolo modificato"
    assert LetterTemplate.model_validate(data).sha256 != original.sha256
