"""Confronto locale per ricovero e copertura delle intestazioni MIMIC-IV-Note."""

import os
import re
from pathlib import Path
from textwrap import indent

from codice_tesi.backend.database import Database
from codice_tesi.discharge.models import MIMIC_DATASET
from codice_tesi.discharge.provenance import TraceResult, render_draft
from codice_tesi.discharge.template import load_template

# Le intestazioni forniscono piste di revisione, non equivalenze cliniche.
PAIRING = {
    "identificazione": ("Name", "Date of Birth", "Service"),
    "data_dimissione": ("Discharge Date", "Date of Discharge"),
    "diagnosi_dimissione": ("Discharge Diagnosis", "Discharge Diagnoses", "Final Diagnosis"),
    "diagnosi_codificate": (),
    "procedure_codificate": (),
    "motivo_ricovero": ("Chief Complaint", "History of Present Illness", "Reason for Admission"),
    "problemi_non_attivi": ("Past Medical History",),
    "procedure_diagnostiche_terapeutiche": (
        "Major Surgical or Invasive Procedure",
        "Procedures",
        "Pertinent Results",
    ),
    "esami_laboratorio": ("Pertinent Results", "Laboratory Results", "Labs"),
    "decorso": ("Brief Hospital Course", "Hospital Course"),
    "condizioni_dimissione": ("Discharge Condition", "Discharge Disposition"),
    "programma_followup": (
        "Discharge Instructions",
        "Followup Instructions",
        "Follow-up Instructions",
    ),
    "terapia_dimissione": ("Discharge Medications", "Medications on Discharge"),
    "contatti_urgenze": (
        "Discharge Instructions",
        "Followup Instructions",
        "Follow-up Instructions",
    ),
    "istologico_in_attesa": ("Pending Results", "Pending Tests", "Pending Studies"),
    "informazioni_istologico": ("Pending Results", "Pending Tests", "Pending Studies"),
    "medici": ("Attending",),
}
HEADERS = sorted({header for aliases in PAIRING.values() for header in aliases})
HEADER_PATTERN = re.compile(
    r"(?:^|(?<= {2}))[ \t]*(" + "|".join(map(re.escape, HEADERS)) + r")[ \t]*:",
    re.IGNORECASE | re.MULTILINE,
)


def matched_headers(text: str) -> set[str]:
    return {match.group(1).casefold() for match in HEADER_PATTERN.finditer(text)}


def mapped_headers(text: str) -> dict[str, tuple[str, ...]]:
    found = matched_headers(text)
    return {
        section: tuple(header for header in aliases if header.casefold() in found)
        for section, aliases in PAIRING.items()
    }


async def profile_mimic(database: Database) -> None:
    async with database.connection(read_only=True) as connection:
        cursor = await connection.execute("SELECT notes FROM mimic_discharge_references")
        notes = [row["notes"] for row in await cursor.fetchall()]
    print(f"Ricoveri abbinati: {len(notes)}. Lettere originali: {sum(map(len, notes))}.")
    header_sets = [matched_headers(revisions[-1]["text"]) for revisions in notes]
    for header in HEADERS:
        count = sum(header.casefold() in found for found in header_sets)
        if count:
            print(f"{header}: {count}/{len(notes)}")


async def read_reference_notes(database: Database, context_id: str, version: str) -> list[dict]:
    async with database.connection(read_only=True) as connection:
        cursor = await connection.execute(
            "SELECT r.notes FROM mimic_discharge_references r "
            "JOIN composition_contexts c ON c.id = r.context_id "
            "WHERE c.id = %s AND c.payload->>'dataset' = %s "
            "AND c.payload->>'dataset_version' = %s",
            (context_id, MIMIC_DATASET, version),
        )
        row = await cursor.fetchone()
    return row["notes"] if row else []


async def compare_mimic(database: Database, draft_path: Path, destination: Path) -> None:
    draft = TraceResult.model_validate_json(draft_path.read_bytes())
    binding = draft.binding
    template = load_template()
    if (
        binding.dataset.id != MIMIC_DATASET
        or draft.validation.technical != "passed"
        or binding.template.sha256 != template.sha256
        or [section.section_id for section in draft.draft.sections] != list(PAIRING)
    ):
        raise ValueError("Serve una bozza MIMIC validata con il template Campus corrente.")

    notes = await read_reference_notes(database, binding.context_id, binding.dataset.version)
    if not notes:
        raise ValueError("Nessun originale per il ricovero e la versione di questa bozza.")

    note = notes[-1]
    headers = mapped_headers(note["text"])
    checklist = "\n".join(
        f"| {section.title} | {', '.join(headers[section.id]) or '—'} |"
        for section in template.sections
    )
    content = (
        "# Confronto locale CAMPUS / MIMIC-IV-Note\n\n"
        f"Ricovero `{binding.context_id}` · Esecuzione `{binding.run_id}`\n\n"
        f"Riferimento `{note['note_id']}` (ultimo per storetime, charttime e note_id "
        f"fra {len(notes)} versioni abbinate).\n\n"
        "Le intestazioni sono indizi strutturali. Per la revisione verificare fatti omessi, "
        "fatti aggiunti, contraddizioni e contenuti privi di evidenza nelle fonti ammesse.\n\n"
        "| Campo CAMPUS | Intestazioni rilevate nella lettera originale |\n"
        "|---|---|\n"
        f"{checklist}\n\n"
        "## Bozza CAMPUS in italiano\n\n"
        + indent(render_draft(draft.draft, template), "    ")
        + "\n\n## Lettera originale MIMIC in inglese\n\n"
        + indent(note["text"], "    ")
        + "\n"
    )
    with os.fdopen(
        os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8"
    ) as file:
        file.write(content)
