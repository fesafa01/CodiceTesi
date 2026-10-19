"""Fixture neutre: template Campus reale, episodi verificati e risposte simulabili."""

import hashlib

import pytest

from codice_tesi.discharge.models import CompositionPacket, Context, EvidenceSource
from codice_tesi.discharge.template import load_template


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
