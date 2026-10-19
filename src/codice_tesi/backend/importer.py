"""Importazione eCREAM: raggruppamento per note_id e salvataggio atomico."""

import hashlib
import json
import re
from pathlib import Path

from codice_tesi.backend.database import Database
from codice_tesi.backend.repository import save_dataset
from codice_tesi.discharge.models import Context, EvidenceSource

KINDS = {
    "TRIAGE": "triage",
    "ANAMNESIS": "anamnesi",
    "MEDICAL_VISIT": "visita",
    "CLINICAL_DIARY": "diario",
    "SPECIALIST_CONSULTANCY": "consulenza",
    "NURSING_CARE_NOTES": "assistenza",
    "DISCHARGE": "dimissione",
}


def load_ecream(path: Path) -> tuple[str, list[tuple[Context, EvidenceSource]]]:
    """Raggruppa per note_id e deduplica i testi identici ripetuti per annotazioni."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    groups = {}
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        note_id, text = row.get("note_id"), row.get("text")
        if type(note_id) is not int or not isinstance(text, str) or not text.strip():
            raise ValueError(f"Nota eCREAM non valida alla riga {line_number}.")
        tags = re.findall(r"---([A-Z_]+)---", text)
        if len(tags) != 1 or tags[0] not in KINDS:
            raise ValueError(f"Tipo nota eCREAM non riconosciuto: {note_id}.")
        notes = groups.setdefault(note_id, {})
        entry = notes.setdefault(text, {"kind": KINDS[tags[0]], "origins": set()})
        entry["origins"].add(f"ecream-gamma-span/{row.get('split', 'unknown')}/note/{note_id}")
    loaded = []
    for note_id, notes in sorted(groups.items()):
        multiple = len(notes) > 1
        context = Context(
            id=f"ecream-note-{note_id}",
            dataset="ecream-gamma-span",
            dataset_version=digest,
            scope="gruppo_note" if multiple else "nota_isolata",
            limitations=(
                f"Contesto basato su {len(notes)} testi distinti dello stesso note_id.",
                "Il note_id non certifica l'identità di un paziente o di un episodio clinico.",
                "Le annotazioni CRF non sono usate come fatti clinici o risposte attese.",
            ),
        )
        for text, entry in sorted(
            notes.items(), key=lambda pair: hashlib.sha256(pair[0].encode()).hexdigest()
        ):
            sha = hashlib.sha256(text.encode()).hexdigest()
            source = EvidenceSource(
                id=f"ecream-{note_id}-{sha}" if multiple else f"ecream-{note_id}",
                context_id=context.id,
                kind=entry["kind"],
                text=text,
                origin="; ".join(sorted(entry["origins"])),
                version=digest,
                sha256=sha,
            )
            loaded.append((context, source))
    if not loaded:
        raise ValueError("Il file eCREAM non contiene note.")
    return digest, loaded


async def import_ecream(database: Database, path: Path) -> tuple[bool, int]:
    """Converte le note e salva contesti e fonti in una sola transazione."""
    _, loaded = load_ecream(path)
    return await save_dataset(
        database,
        list({context.id: context for context, _ in loaded}.values()),
        [source for _, source in loaded],
    )
