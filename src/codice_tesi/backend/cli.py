"""Avvio del servizio e importazione esplicita dei dati clinici."""

import argparse
import asyncio
import logging
from pathlib import Path

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from codice_tesi.backend.database import BackendSettings, Database, initialize_database
from codice_tesi.backend.synthea import import_synthea


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backend documentale clinico della tesi.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Crea le tabelle senza importare dati.")
    commands.add_parser("serve", help="Avvia l'API dei pacchetti di compilazione.")
    synthea = commands.add_parser(
        "import-synthea", help="Importa i ricoveri FHIR Synthea indicizzati."
    )
    synthea.add_argument("path", type=Path, help="Cartella con indice.json e FHIR originali.")
    synthea.add_argument(
        "--replace", action="store_true", help="Sostituisce i contesti attivi del DB con Synthea."
    )
    args = parser.parse_args(argv)
    try:
        settings = BackendSettings()
        logging.basicConfig(level=settings.log_level, format="%(message)s")
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
        else:
            asyncio.run(run_command(settings, args))
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        parser.exit(2, f"Configurazione non valida: controllare {fields}.\n")
    except (ValueError, OSError) as exc:
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
        elif args.command == "import-synthea":
            changed, count = await import_synthea(database, args.path, replace=args.replace)
            print(f"Synthea: {count} ricoveri. " + ("Importati." if changed else "Già presenti."))
