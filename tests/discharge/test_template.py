"""Il template è l'unica definizione di struttura, regole e sottocampi."""

import pytest
from pydantic import ValidationError

from codice_tesi.discharge.template import LetterTemplate, load_template


def test_template_covers_campus_fields_and_requirements_from_annotated_form():
    template = load_template()
    assert template.id == "campus-discharge" and template.version == "1.1.0"
    assert len(template.sections) == 17 and len(template.sha256) == 64
    requirements = {
        "identificazione": "required",
        "data_dimissione": "unconfirmed",
        "diagnosi_dimissione": "optional",
        "diagnosi_codificate": "required",
        "procedure_codificate": "required",
        "motivo_ricovero": "required",
        "problemi_non_attivi": "required",
        "procedure_diagnostiche_terapeutiche": "required",
        "esami_laboratorio": "optional",
        "decorso": "required",
        "condizioni_dimissione": "required",
        "programma_followup": "optional",
        "terapia_dimissione": "required",
        "contatti_urgenze": "required",
        "istologico_in_attesa": "required",
        "informazioni_istologico": "conditional",
        "medici": "optional",
    }
    assert {s.id: s.requirement.status for s in template.sections} == requirements
    pending = template.section("istologico_in_attesa")
    assert not pending.repeatable
    assert len(pending.fields) == 1 and pending.fields[0].type == "boolean"
    assert pending.fields[0].required_for_completeness
    assert template.section("informazioni_istologico").applies_when.model_dump() == {
        "section_id": "istologico_in_attesa",
        "field_id": "attesa",
        "equals": True,
    }
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
