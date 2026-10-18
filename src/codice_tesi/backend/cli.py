"""Comandi operativi del backend e controllo separato degli scenari."""

import argparse
import asyncio
import json
import logging

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from codice_tesi.backend.config import BackendSettings
from codice_tesi.backend.control import set_state
from codice_tesi.backend.database import Database, initialize_database
from codice_tesi.backend.dataset_types import aware
from codice_tesi.backend.importer import import_dataset
from codice_tesi.backend.repository import ClinicalError, snapshot


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backend clinico sintetico della tesi.")
    parser.add_argument(
        "--dataset-dir", help="Directory esterna del dataset da validare/importare."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "import-dataset", help="Importa esplicitamente un dataset in una transazione."
    )
    commands.add_parser("init", help="Crea soltanto le tabelle, senza caricare dati clinici.")
    commands.add_parser("validate-dataset", help="Valida il dataset senza scrivere nel database.")
    commands.add_parser("serve", help="Avvia l'API clinica di sola lettura.")
    scenario = commands.add_parser("scenario", help="Controllo del simulatore, separato dall'API.")
    controls = scenario.add_subparsers(dest="control", required=True)
    controls.add_parser("list", help="Elenca gli scenari per il ricercatore.")
    controls.add_parser("status", help="Mostra stato e orologio del simulatore.")
    select_scenario = controls.add_parser("select", help="Seleziona e ripristina uno scenario.")
    select_scenario.add_argument("id")
    advance = controls.add_parser("advance", help="Avanza l'orologio nella stessa esecuzione.")
    advance.add_argument("--at", required=True, help="Data ISO 8601 con fuso orario.")
    args = parser.parse_args(argv)
    try:
        overrides = {"dataset_dir": args.dataset_dir} if args.dataset_dir else {}
        settings = BackendSettings(**overrides)
        logging.basicConfig(level=settings.log_level, format="%(message)s")
        if args.command in {"validate-dataset", "import-dataset"} and settings.dataset_dir is None:
            raise ValueError("Specificare --dataset-dir oppure CLINICAL_DATASET_DIR.")
        if args.command == "validate-dataset":
            from codice_tesi.backend.dataset import Dataset

            dataset = Dataset(settings.dataset_dir)
            loaded = dataset.load()
            print(
                json.dumps(
                    {
                        "versione": loaded[0]["versione"],
                        "pazienti": len(loaded[2]),
                        "fonti_base": len(loaded[6]),
                        "scenari": sorted(dataset.scenarios),
                        "file_verificati": len(dataset.loaded),
                    },
                    ensure_ascii=False,
                )
            )
            return
        if args.command == "serve":
            import uvicorn

            from codice_tesi.backend.app import create_app

            uvicorn.run(
                create_app(settings),
                host=settings.host,
                port=settings.port,
                log_level=settings.log_level.lower(),
                access_log=False,
            )
            return
        asyncio.run(run_command(settings, args))
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        parser.exit(2, f"Configurazione non valida: controllare {fields}.\n")
    except (ValueError, OSError, ClinicalError) as exc:
        parser.exit(2, f"Operazione non completata: {exc}\n")
    except (DatabaseError, PoolTimeout) as exc:
        parser.exit(
            1, f"Database non disponibile o schema non aggiornato ({type(exc).__name__}).\n"
        )
    except KeyboardInterrupt:
        parser.exit(130)


async def run_command(settings, args):
    async with Database(settings) as database:
        if args.command == "init":
            await initialize_database(database)
            print("Schema inizializzato. Nessun dataset importato.")
            return
        if args.command == "import-dataset":
            changed = await import_dataset(database, settings.dataset_dir)
            print("Dataset importato." if changed else "Dataset già inizializzato e coerente.")
            return
        async with database.connection() as connection:
            if args.control == "list":
                cursor = await connection.execute(
                    "SELECT id, description AS descrizione FROM scenarios ORDER BY id"
                )
                data = await cursor.fetchall()
            else:
                if args.control == "select":
                    view = await set_state(connection, scenario_id=args.id)
                elif args.control == "advance":
                    view = await set_state(connection, at=aware(args.at))
                else:
                    view = await snapshot(connection)
                data = {
                    "scenario": view.scenario,
                    "esecuzione_id": view.run_id,
                    "revisione": view.revision,
                    "istante": view.time.isoformat(),
                }
            print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
