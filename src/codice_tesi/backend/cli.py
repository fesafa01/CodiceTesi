"""Avvio del servizio e importazione esplicita delle note eCREAM."""

import argparse
import asyncio
import logging
from pathlib import Path

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from codice_tesi.backend.database import BackendSettings, Database, initialize_database
from codice_tesi.backend.importer import import_ecream


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backend documentale eCREAM della tesi.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Crea le tabelle senza importare dati.")
    commands.add_parser("serve", help="Avvia l'API dei pacchetti di compilazione.")
    ecream = commands.add_parser("import-ecream", help="Importa contesti e fonti da notes.jsonl.")
    ecream.add_argument("path", type=Path, help="Percorso esplicito al file notes.jsonl.")
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
        else:
            changed, count = await import_ecream(database, args.path)
            print(f"eCREAM: {count} contesti. " + ("Importati." if changed else "Già presenti."))
