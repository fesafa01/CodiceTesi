"""Prova manuale di conservazione/cancellazione su nuove copie locali sintetiche.

Richiede RETENTION_DEMO_POSTGRES_URL su un PostgreSQL locale temporaneo.
Non usa il DB del prototipo, non chiama LLM, non modifica l'orologio di sistema.
La cartella di output deve essere nuova. Log, tracce, metriche e cache sono
fixture su file: non vengono cancellati i servizi di osservabilità in esercizio.
"""

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from codice_tesi.backend.database import BackendSettings, Database, initialize_database
from codice_tesi.backend.repository import save_dataset
from codice_tesi.backend.synthea import load_synthea
from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.template import load_template
from codice_tesi.retention_demo import (
    MARKER,
    create_backup,
    create_sandbox,
    purge_copies,
    register_database_copy,
    restore_backup,
    write_copy,
)


def hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def exercise(root, settings, packets):
    now = datetime(2026, 10, 26, 10, tzinfo=UTC)
    markers = ("RETENTION_EXPIRED_SYNTHETIC", "RETENTION_FRESH_SYNTHETIC")
    stages = []
    async with Database(settings) as database:
        await initialize_database(database)
        await save_dataset(
            database,
            [packet.context for packet in packets],
            [source for packet in packets for source in packet.sources],
        )
        for index, (packet, label) in enumerate(zip(packets[:2], ("old", "fresh"), strict=True)):
            created = now - timedelta(days=8) if label == "old" else now
            register_database_copy(root, packet.context.id, created_at=created)
            write_copy(
                root,
                "sources",
                label + ".json",
                packet.model_dump_json().encode(),
                created_at=created,
            )
            for suffix in ("richiesta", "bozza", "parziale"):
                write_copy(
                    root,
                    "artifacts",
                    label + "-" + suffix + ".txt",
                    markers[index].encode(),
                    created_at=created,
                )
            for suffix in ("log", "trace", "metric", "cache"):
                write_copy(
                    root,
                    "telemetry",
                    label + "-" + suffix + ".txt",
                    markers[index].encode(),
                    created_at=created,
                )
            registry = json.dumps(
                {"run_id": "demo-" + label, "status": "fixture", "clinical_text": False}
            ).encode()
            write_copy(
                root,
                "registry",
                label + ".json",
                registry,
                created_at=now - timedelta(days=31) if label == "old" else now,
            )
            create_backup(root, "sources/" + label + ".json", label + ".json", created_at=created)

        async def present_contexts():
            async with database.connection(read_only=True) as connection:
                cursor = await connection.execute("SELECT id FROM composition_contexts ORDER BY id")
                return [row["id"] for row in await cursor.fetchall()]

        before = hashes(root)
        preview = await purge_copies(root, as_of=now)
        assert hashes(root) == before and len(await present_contexts()) == 3
        stages.append({"name": "preview", "unchanged": True, **preview})
        for name, clock in (
            ("initial", now),
            ("24-hours", now + timedelta(days=1)),
            ("7-days", now + timedelta(days=7)),
            ("30-days", now + timedelta(days=30)),
        ):
            result = await purge_copies(root, as_of=clock, apply=True, database=database)
            present = await present_contexts()
            assert packets[2].context.id in present  # Controllo fuori inventario.
            if name == "initial":
                assert packets[0].context.id not in present and packets[1].context.id in present
                assert not any(
                    markers[0].encode() in p.read_bytes() for p in root.rglob("*") if p.is_file()
                )
                restored = restore_backup(
                    root, "backups/fresh.json", "restored.json", as_of=now + timedelta(hours=6)
                )
                assert CompositionPacket.model_validate_json(restored.read_bytes()) == packets[1]
                manifest = json.loads((root / MARKER).read_text())
                expiry = {e["path"]: e["expires_at"] for e in manifest["files"]}
                assert expiry["sources/restored.json"] == expiry["sources/fresh.json"]
                result["backup_restored_with_inherited_deadline"] = True
            if name == "24-hours":
                assert packets[1].context.id in present and (root / "registry/fresh.json").exists()
                assert not list((root / "artifacts").iterdir()) and not list(
                    (root / "telemetry").iterdir()
                )
                assert (root / "sources/fresh.json").exists() and (
                    root / "backups/fresh.json"
                ).exists()
            if name == "7-days":
                assert present == [packets[2].context.id]
                assert not list((root / "sources").iterdir()) and not list(
                    (root / "backups").iterdir()
                )
                assert (root / "registry/fresh.json").exists()
                try:
                    restore_backup(root, "backups/fresh.json", "forbidden.json", as_of=clock)
                except ValueError:
                    result["expired_backup_restore_denied"] = True
                else:
                    raise AssertionError("Ripristino del backup scaduto consentito.")
                assert not any(
                    markers[1].encode() in p.read_bytes() for p in root.rglob("*") if p.is_file()
                )
            if name == "30-days":
                assert [p.name for p in root.rglob("*") if p.is_file()] == [MARKER]
            stages.append({"name": name, "remaining_contexts": present, **result})
        repeated = await purge_copies(
            root, as_of=now + timedelta(days=30), apply=True, database=database
        )
        assert not repeated["planned_files"] and not repeated["planned_contexts"]
        stages.append({"name": "repeat", "idempotent": True, **repeated})
    return stages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--synthetic-data", type=Path, default=Path("../datasets_pubblici/synthea_dimissioni")
    )
    args = parser.parse_args()
    url = os.getenv("RETENTION_DEMO_POSTGRES_URL", "")
    parsed = urlsplit(url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != 15435:
        parser.error(
            "Usare RETENTION_DEMO_POSTGRES_URL sul PostgreSQL temporaneo locale, porta 15435."
        )
    original_hashes = hashes(args.synthetic_data)
    _, loaded = load_synthea(args.synthetic_data)
    contexts = {context.id: context for context, _ in loaded}
    selected = sorted(contexts)[:3]
    if len(selected) != 3:
        parser.error("Servono almeno tre contesti sintetici per la prova.")
    packets = []
    for index, context_id in enumerate(selected):
        marker = ("RETENTION_EXPIRED_SYNTHETIC", "RETENTION_FRESH_SYNTHETIC", "RETENTION_CONTROL")[
            index
        ]
        sources = []
        for context, source in loaded:
            if context.id == context_id:
                text = source.text + "\n" + marker
                sources.append(
                    source.model_copy(
                        update={
                            "text": text,
                            "sha256": hashlib.sha256(text.encode()).hexdigest(),
                            "version": "retention-demo-v1",
                        }
                    )
                )
        packets.append(
            CompositionPacket(
                context=contexts[context_id].model_copy(
                    update={"dataset_version": "retention-demo-v1"}
                ),
                sources=tuple(sorted(sources, key=lambda source: source.id)),
                template=load_template(),
            )
        )
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output / "sandbox"
    schema = create_sandbox(root)
    settings = BackendSettings(database_url=url, database_schema=schema, _env_file=None)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        stages = asyncio.run(exercise(root, settings, packets))
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    assert hashes(args.synthetic_data) == original_hashes
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "policy_version": "demo-1.0",
        "clock": "simulated; no system clock change",
        "inference_performed": False,
        "original_files_checked": len(original_hashes),
        "originals_unchanged": True,
        "database_scope": schema,
        "temporary_schema_removed": True,
        "telemetry_scope": "file fixtures, not live Phoenix/Prometheus/Collector stores",
        "stages": stages,
        "passed": True,
    }
    (args.output / "risultati.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print("Verifica superata: anteprima, scadenze, backup, PostgreSQL e controllo degli originali.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
