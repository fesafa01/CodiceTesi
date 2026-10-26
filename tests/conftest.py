"""Fixture neutre: template Campus reale, episodi verificati e risposte simulabili."""

import hashlib

import pytest

from codice_tesi.discharge.models import CompositionPacket, Context, EvidenceSource
from codice_tesi.discharge.prompts import MISSING_VALUE
from codice_tesi.discharge.template import load_template


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
