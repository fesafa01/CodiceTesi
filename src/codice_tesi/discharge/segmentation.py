"""Passaggi deterministici nel testo originale, senza riscrittura o tagli di frasi."""

import re

from codice_tesi.discharge.models import EvidenceSource

SEGMENTATION_VERSION = "passaggi-v1"


def source_intervals(source: EvidenceSource) -> list[tuple[int, int]]:
    """Eventi della cronologia per riga; note per paragrafi, senza perdere contesto.

    Le righe della cronologia Synthea sono record autosufficienti preparati
    dall'adattatore. Le note brevi rimangono intere. Un paragrafo lungo non viene
    spezzato arbitrariamente: un limite di risposta dei tool lo segnala.
    Gli intervalli includono tutti i caratteri, anche separatori e Unicode.
    """
    text = source.text
    if source.kind == "dati_ricovero":
        boundaries = [match.end() for match in re.finditer(r"\n", text)]
    elif len(text) > 1200:
        boundaries = [match.end() for match in re.finditer(r"\n[ \t]*\n", text)]
    else:
        return [(0, len(text))]
    intervals = []
    start = 0
    for end in [*boundaries, len(text)]:
        if end <= start:
            continue
        if text[start:end].strip():
            intervals.append((start, end))
            start = end
    if start < len(text) and intervals:
        intervals[-1] = (intervals[-1][0], len(text))
    return intervals or [(0, len(text))]
