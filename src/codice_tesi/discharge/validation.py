"""Controlli di forma della bozza; il sostegno clinico resta da verificare sulle fonti."""

import re

from codice_tesi.discharge.prompts import MISSING_VALUE
from codice_tesi.discharge.template import LetterTemplate


def validate_draft(text: str, template: LetterTemplate) -> None:
    """Richiede le sezioni canoniche e rispetta il binario istologico e i suoi dettagli.

    Una risposta non documentata resta una lacuna ammessa nella bozza. Questo
    controllo non accerta se il sì/no prodotto dal modello sia fedele alle fonti.
    """
    headings = [f"{index}. {section.title}" for index, section in enumerate(template.sections, 1)]
    pattern = r"^[ \t]*(?:" + "|".join(re.escape(title) for title in headings) + r")[ \t]*$"
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if [match.group().strip() for match in matches] != headings:
        raise ValueError(
            "La bozza deve contenere tutte le intestazioni Campus una volta in ordine."
        )
    content = {
        section.id: text[
            match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)
        ].strip()
        for index, (section, match) in enumerate(zip(template.sections, matches, strict=True))
    }
    answer = content["istologico_in_attesa"]
    details = content["informazioni_istologico"]
    if answer != MISSING_VALUE and answer.casefold() not in {"sì", "si", "no"}:
        raise ValueError("L'attesa dell'istologico richiede solo Sì/No o il dato mancante.")
    if answer.casefold() in {"sì", "si"}:
        if not details:
            raise ValueError("Con Sì riportare i dettagli istologici o segnalare il dato mancante.")
    elif details:
        raise ValueError(
            "Con No o risposta non documentata le informazioni istologiche restano vuote."
        )
