"""Importazione atomica del dataset con INSERT PostgreSQL parametrizzate."""

import copy
import hashlib
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from codice_tesi.backend.database import Database
from codice_tesi.backend.dataset import Dataset
from codice_tesi.backend.dataset_types import aware
from codice_tesi.backend.schemas import TemplateField


async def import_dataset(database: Database, directory: Path) -> bool:
    try:
        dataset = Dataset(directory)
        manifest, units, patients, stays, visits, template, base, coverage, digest = dataset.load()
        definitions = dataset.scenarios
        fields = [
            TemplateField.model_validate(
                {key: row[key] for key in TemplateField.model_fields}
            ).model_dump()
            for row in template["campi"]
        ]
    except (KeyError, IndexError, StopIteration, TypeError) as exc:
        raise ValueError("Struttura del dataset incompleta o non valida.") from exc
    # Le definizioni cliniche, non la formattazione del codice, identificano l'importazione.
    scenario_data = {
        id_: {
            "descrizione": description,
            "canali_non_disponibili": unavailable,
            "fonti": [asdict(source) for source in sources],
        }
        for id_, (description, unavailable, sources) in definitions.items()
    }
    digest = hashlib.sha256(
        (digest + json.dumps(scenario_data, sort_keys=True, default=str)).encode()
    ).hexdigest()
    async with database.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("SELECT id, seed_digest FROM scenarios")
        existing = await cursor.fetchall()
        if existing:
            if {s["id"] for s in existing} != set(definitions) or any(
                s["seed_digest"] != digest for s in existing
            ):
                raise ValueError(
                    "Il database contiene un dataset diverso: usare una nuova istanza di sviluppo."
                )
            await cursor.execute("SELECT id FROM simulation_state WHERE id = 1")
            if await cursor.fetchone() is None:
                raise ValueError("Stato del simulatore incompleto.")
            return False
        await cursor.executemany(
            "INSERT INTO clinical_units (id, name, kind) VALUES (%s, %s, %s)",
            [
                (r["id"], r["nome"], kind)
                for key, kind in [("unita_cliniche", "clinica"), ("unita_degenza", "degenza")]
                for r in units[key]
            ],
        )
        for row in patients:
            if row.get("sintetico") is not True:
                raise ValueError("Paziente non dichiarato sintetico.")
            await cursor.execute(
                "INSERT INTO patients (id, given_name, family_name, birth_date) "
                "VALUES (%s, %s, %s, %s)",
                (row["id"], row["nome"], row["cognome"], date.fromisoformat(row["data_nascita"])),
            )
        await cursor.executemany(
            """INSERT INTO episodes (id, patient_id, kind, status, started_at, nosological_id,
               unit_id, ward_id, bed, planned_discharge, discharged_at)
               VALUES (%s, %s, 'ricovero', %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    r["id"],
                    r["paziente_id"],
                    r["stato"],
                    aware(r["ingresso_il"]),
                    r["nosologico"],
                    r["unita_clinica_id"],
                    r["unita_degenza_id"],
                    r["letto"],
                    date.fromisoformat(r["dimissione_prevista_il"])
                    if r["dimissione_prevista_il"]
                    else None,
                    aware(r["dimissione_effettiva_il"]) if r["dimissione_effettiva_il"] else None,
                )
                for r in stays
            ],
        )
        await cursor.executemany(
            """INSERT INTO episodes (id, patient_id, kind, status, started_at, description)
               VALUES (%s, %s, 'prestazione', 'eseguita', %s, %s)""",
            [(r["id"], r["paziente_id"], aware(r["data"]), r["descrizione"]) for r in visits],
        )
        identities = {}
        for _, _, sources in definitions.values():
            for source in sources:
                previous = identities.setdefault(source.identity["id"], source.identity)
                if previous != source.identity:
                    raise ValueError("Identità della fonte incoerente tra scenari.")
        await cursor.executemany(
            """INSERT INTO clinical_sources
               (id, patient_id, episode_id, category, kind, origin, author_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    i["id"],
                    i["patient_id"],
                    i["episode_id"],
                    i["category"],
                    i["kind"],
                    i["origin"],
                    i["author_id"],
                )
                for i in identities.values()
            ],
        )
        await cursor.executemany(
            """INSERT INTO scenarios (id, description, unavailable_channels, initial_time,
               dataset_version, seed_digest) VALUES (%s, %s, %s, %s, %s, %s)""",
            [
                (
                    id_,
                    description,
                    Jsonb(unavailable),
                    aware(manifest["snapshot"]),
                    manifest["versione"],
                    digest,
                )
                for id_, (description, unavailable, _) in definitions.items()
            ],
        )
        await cursor.executemany(
            "INSERT INTO source_channels (source_id, channel) VALUES (%s, %s)",
            [(i["id"], c) for i in identities.values() for c in i["channels"]],
        )
        for scenario_id, (_, _, sources) in definitions.items():
            for source in sources:
                sections = copy.deepcopy(source.sections)
                text = "\n\n".join(f"{s['titolo']}\n{s['testo']}" for s in sections)
                await cursor.execute(
                    """INSERT INTO source_versions (scenario_id, source_id, version, title,
                       clinical_at, recorded_at, available_at, status, text, search_text,
                       sections, pdf, sha256, pdf_size, extraction_status, extraction_method)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        scenario_id,
                        source.identity["id"],
                        source.version,
                        source.title,
                        source.clinical_at,
                        source.recorded_at,
                        source.available_at,
                        source.status,
                        text,
                        (source.title + "\n" + text).casefold(),
                        Jsonb(sections),
                        source.pdf,
                        source.sha256,
                        len(source.pdf) if source.pdf else None,
                        source.extraction_status,
                        source.extraction_method,
                    ),
                )
                await cursor.executemany(
                    """INSERT INTO measurements (scenario_id, source_id, version, ordinal,
                       kind, name, value, unit, clinical_at, unit_code, code, code_system)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [
                        (
                            scenario_id,
                            source.identity["id"],
                            source.version,
                            ordinal,
                            m["kind"],
                            m["name"],
                            m["value"],
                            m["unit"],
                            m["clinical_at"],
                            m.get("unit_code"),
                            m.get("code"),
                            m.get("code_system"),
                        )
                        for ordinal, m in enumerate(source.measurements, 1)
                    ],
                )
        await cursor.executemany(
            "INSERT INTO module_coverage (episode_id, path, status) VALUES (%s, %s, %s)",
            coverage,
        )
        await cursor.execute(
            "INSERT INTO letter_templates (id, version, unit_id, fields) VALUES (%s, %s, %s, %s)",
            (template["id"], template["versione"], template["unita_clinica_id"], Jsonb(fields)),
        )
        run_id, time = str(uuid4()), aware(manifest["snapshot"])
        await cursor.execute(
            """INSERT INTO simulation_state (id, scenario_id, run_id, \"current_time\", revision)
               VALUES (1, %s, %s, %s, 1)""",
            (manifest["scenario_iniziale"], run_id, time),
        )
        await cursor.execute(
            """INSERT INTO state_revisions (revision, scenario_id, run_id, \"current_time\")
               VALUES (1, %s, %s, %s)""",
            (manifest["scenario_iniziale"], run_id, time),
        )
    return True
