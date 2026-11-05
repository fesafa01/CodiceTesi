"""Avvio del servizio e importazione esplicita dei dati clinici."""

import argparse
import asyncio
import logging
import os
import secrets
from pathlib import Path

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from codice_tesi.backend.database import BackendSettings, Database, initialize_database
from codice_tesi.backend.mimic import MAX_CASES, load_mimic
from codice_tesi.backend.mimic_comparison import compare_mimic, profile_mimic
from codice_tesi.backend.repository import save_dataset


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backend documentale clinico della tesi.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Crea le tabelle senza importare dati.")
    commands.add_parser("serve", help="Avvia l'API dei pacchetti di compilazione.")
    commands.add_parser("serve-reference", help="Avvia l'API delle lettere originali.")
    mimic = commands.add_parser("import-mimic", help="Importa un campione MIMIC-IV abbinato.")
    mimic.add_argument("hosp", type=Path, help="Cartella hosp con i CSV compressi.")
    mimic.add_argument("notes", type=Path, help="Cartella note con discharge e radiology.")
    mimic.add_argument("--ed", type=Path, help="Cartella ED 2.2 per il campione misto.")
    mimic.add_argument("--icu", type=Path, help="Cartella ICU 2.2 per il campione misto.")
    mimic.add_argument("--limit", type=int, default=20, help=f"Ricoveri (1–{MAX_CASES}).")
    mimic.add_argument("--seed", type=int, default=42, help="Seed di selezione riproducibile.")
    mimic.add_argument("--replace", action="store_true", help="Sostituisce il solo campione MIMIC.")
    mimic.add_argument(
        "--access-file", type=Path, help="Scrive token e assegnazioni locali in un file privato."
    )
    commands.add_parser("profile-mimic", help="Conta le intestazioni nei riferimenti, senza testo.")
    comparison = commands.add_parser(
        "compare-mimic", help="Abbina una bozza CAMPUS validata al riferimento originale."
    )
    comparison.add_argument(
        "draft", type=Path, help="File bozza_strutturata.json della generazione."
    )
    comparison.add_argument("destination", type=Path, help="Nuovo report Markdown privato.")
    args = parser.parse_args(argv)
    try:
        settings = BackendSettings()
        logging.basicConfig(level=settings.log_level, format="%(message)s")
        if args.command in ("serve", "serve-reference"):
            import uvicorn

            from codice_tesi.backend.app import create_app

            uvicorn.run(
                create_app(settings, reference_only=args.command == "serve-reference"),
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
    if args.command == "import-mimic":
        contexts, sources, references = load_mimic(
            args.hosp, args.notes, limit=args.limit, seed=args.seed, ed=args.ed, icu=args.icu
        )
        if args.ed is not None:
            validate_mimic_requests(contexts, sources)
    async with Database(settings) as database:
        if args.command == "init":
            await initialize_database(database)
            print("Schema inizializzato. Nessun dataset importato.")
        elif args.command == "import-mimic":
            await initialize_database(database)
            changed, count = await save_dataset(
                database, contexts, sources, replace=args.replace, references=references
            )
            if args.access_file:
                write_mimic_access(args.access_file, contexts)
            print(f"MIMIC-IV: {count} ricoveri. " + ("Importati." if changed else "Già presenti."))
        elif args.command == "profile-mimic":
            await profile_mimic(database)
        elif args.command == "compare-mimic":
            await compare_mimic(database, args.draft, args.destination)
            print(f"Confronto salvato: {args.destination}")


def validate_mimic_requests(contexts, sources) -> None:
    from codice_tesi.config import ModelConfig
    from codice_tesi.discharge.config import ContextConfig
    from codice_tesi.discharge.context import prepare_request
    from codice_tesi.discharge.models import CompositionPacket
    from codice_tesi.discharge.template import load_template

    model = ModelConfig(thinking=False)
    template = load_template()
    estimates = []
    for context in contexts:
        packet = CompositionPacket(
            context=context,
            sources=tuple(source for source in sources if source.context_id == context.id),
            template=template,
        )
        request = prepare_request(
            packet, ContextConfig(), output_tokens=model.max_tokens, model_config=model
        )
        estimates.append(request.input_tokens_estimate)
    print(
        f"Pacchetti verificati senza inferenza: {len(estimates)}, "
        f"{min(estimates)}–{max(estimates)} token di input stimati."
    )


def write_mimic_access(path: Path, contexts) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    token = secrets.token_urlsafe(32)
    assignments = ",".join(sorted(context.id for context in contexts))
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(f"APP_MIMIC_TOKEN={token}\nAPP_MIMIC_CONTEXTS={assignments}\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
