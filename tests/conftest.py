"""Fixture neutre: template Campus reale, episodi verificati e risposte simulabili."""

import hashlib
import json

import pytest

from codice_tesi.discharge.models import CompositionPacket, Context, EvidenceSource
from codice_tesi.discharge.template import MISSING_VALUE, load_template


@pytest.fixture
def response_factory():
    """Serializza le lettere di fixture con riferimenti al catalogo di packet_factory."""

    def create(letter):
        template = load_template()
        headings = [f"{i}. {section.title}\n" for i, section in enumerate(template.sections, 1)]
        sections = []
        for index, (section, heading) in enumerate(zip(template.sections, headings, strict=True)):
            start = letter.index(heading) + len(heading)
            end = letter.index(headings[index + 1]) if index + 1 < len(headings) else len(letter)
            text = letter[start:end].strip()
            sources = (
                [{"source_id": "source-0", "evidence_ids": ["evidenza-0001"]}]
                if text and text != MISSING_VALUE
                else []
            )
            sections.append({"section_id": section.id, "text": text, "sources": sources})
        return json.dumps({"sections": sections}, ensure_ascii=False, indent=2)

    return create


@pytest.fixture
def letter_factory():
    """Bozza con intestazioni reali e casi istologici indipendenti dalle fonti di test."""

    def create(*, answer=MISSING_VALUE, details=""):
        values = {"istologico_in_attesa": answer, "informazioni_istologico": details}
        return "BOZZA DI LETTERA DI DIMISSIONE - DA REVISIONARE\n" + "\n".join(
            f"{index}. {section.title}\n{values.get(section.id, 'Testo di fixture.')}\n"
            for index, section in enumerate(load_template().sections, 1)
        )

    return create


@pytest.fixture
def packet_factory():
    """Costruisce casi indipendenti dal dataset, riusando lo stesso template versionato."""

    def create(*kinds, text="Dolore da tre giorni.", isolated=False):
        context = Context(
            id="episode-x",
            dataset="fixture",
            dataset_version="1",
            scope="nota_isolata" if isolated else "episodio",
            patient_id=None if isolated else "patient-x",
            episode_id=None if isolated else "episode-x",
        )
        sources = [
            EvidenceSource(
                id=f"source-{i}",
                context_id=context.id,
                kind=kind,
                text=text,
                origin="fixture",
                version="1",
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
            for i, kind in enumerate(kinds)
        ]
        return CompositionPacket(context=context, sources=tuple(sources), template=load_template())

    return create
