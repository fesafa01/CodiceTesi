"""Anteprima parziale Unicode senza pubblicare una bozza validata."""

import json

import pytest

from codice_tesi.discharge.template import load_template
from codice_tesi.web_api.drafts import preview_sections


@pytest.mark.parametrize("fenced", [False, True])
def test_preview_decodes_every_prefix_of_strings_and_sections_without_sources(fenced):
    template = load_template()
    texts = ['Caffè 👁\nNon dolore. "Citazione" e \\ terapia', "Seconda sezione."]
    raw = json.dumps(
        {
            "sections": [
                {"section_id": section.id, "text": text, "sources": []}
                for section, text in zip(template.sections, texts)
            ]
        },
        ensure_ascii=True,
    )
    if fenced:
        raw = "```json\n" + raw + "\n```"
    for end in range(len(raw) + 1):
        preview = preview_sections(raw[:end], template)
        for index, item in enumerate(preview):
            assert item["section_id"] == template.sections[index].id
            assert texts[index].startswith(item["text"])
            assert set(item) == {"section_id", "text"}
    assert [s["text"] for s in preview_sections(raw, template)] == texts


@pytest.mark.parametrize(
    "raw",
    [
        "Consulto una fonte prima di scrivere.",
        '{"reasoning_content":"PRIVATE_REASONING"}',
        '{"tool_calls":[{"function":{"name":"leggi_evidenze"}}]}',
        '{"sections":[{"section_id":"altra-sezione","text":"errato"}]}',
        '{"sections":{}}',
        '"testo"',
        '{"sections":[null]}',
        '{"sections":[{"section_id":"paziente","text":4}]}',
    ],
)
def test_preview_does_not_treat_other_content_as_section_text(raw):
    assert preview_sections(raw, load_template()) == []
