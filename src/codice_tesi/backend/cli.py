"""Avvio del servizio e importazione esplicita dei dati clinici."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from codice_tesi.backend.database import BackendSettings, Database, initialize_database
from codice_tesi.backend.mimic import MAX_CASES, load_mimic
from codice_tesi.backend.mimic_audit import audit_mimic
from codice_tesi.backend.mimic_comparison import compare_mimic, profile_mimic
from codice_tesi.backend.mimic_coverage import audit_candidate_coverage
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
    mimic.add_argument(
        "--hosp-ed-only",
        action="store_true",
        help="Bilancia Hosp e Hosp + ED, escludendo i passaggi in ICU da Hosp/transfers.",
    )
    mimic.add_argument("--limit", type=int, default=20, help=f"Ricoveri (1–{MAX_CASES}).")
    mimic.add_argument("--seed", type=int, default=42, help="Seed di selezione riproducibile.")
    mimic.add_argument(
        "--max-input-tokens",
        type=int,
        help="Tetto per candidato HOSP/HOSP + ED, con stima prudenziale per i tre modelli.",
    )
    mimic.add_argument(
        "--min-discharge-cases",
        type=int,
        default=0,
        help="Minimo di ricoveri con ordini di dimissione utilizzabili nel campione misto.",
    )
    mimic.add_argument(
        "--min-complete-cases",
        type=int,
        default=0,
        help="Minimo di ricoveri con Discharge Med, candidato POE di follow-up, ED e ICU.",
    )
    mimic.add_argument(
        "--rich-pilot",
        action="store_true",
        help="Priorità alla copertura, fonti estese ed etichette del pilota.",
    )
    mimic.add_argument("--replace", action="store_true", help="Sostituisce il solo campione MIMIC.")
    mimic.add_argument(
        "--access-file", type=Path, help="Scrive token e assegnazioni locali in un file privato."
    )
    clinical = commands.add_parser(
        "add-clinical-candidates",
        help="Aggiunge una rosa clinica separata, conservando il campione MIMIC presente.",
    )
    clinical.add_argument("hosp", type=Path)
    clinical.add_argument("notes", type=Path)
    clinical.add_argument("--ed", type=Path, required=True)
    clinical.add_argument("--limit", type=int, default=10)
    clinical.add_argument("--seed", type=int, default=20261109)
    clinical.add_argument(
        "--min-input-tokens",
        type=int,
        default=0,
        help="Floor sul minimo delle tre stime; 0 lo disattiva.",
    )
    clinical.add_argument(
        "--max-input-tokens",
        type=int,
        default=80000,
        help="Ceiling sul massimo delle tre stime prudenziali.",
    )
    clinical.add_argument(
        "--pool-per-profile",
        type=int,
        default=30,
        help="Pazienti da approfondire per ciascun profilo prima del filtro token.",
    )
    clinical.add_argument("--expect-existing", type=int, default=30)
    clinical.add_argument(
        "--source-profile",
        choices=("clinical", "complete"),
        default="clinical",
        help="Selezione clinica di righe intere o intero estratto nel prompt.",
    )
    clinical.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepara e verifica la rosa senza aggiungerla al database.",
    )
    clinical.add_argument("--report-dir", type=Path, required=True)
    clinical.add_argument(
        "--access-file",
        type=Path,
        required=True,
        help="File esistente: conserva token e assegnazioni e aggiunge i nuovi casi.",
    )
    audit = commands.add_parser(
        "audit-mimic", help="Confronta gli ordini nei CSV Hosp con il campione PostgreSQL."
    )
    audit.add_argument("hosp", type=Path, help="Cartella hosp con i CSV compressi.")
    audit.add_argument("--expect", type=int, default=30, help="Ricoveri attesi in PostgreSQL.")
    coverage = commands.add_parser(
        "audit-mimic-candidates", help="Misura la copertura delle fonti nei candidati MIMIC."
    )
    coverage.add_argument("hosp", type=Path)
    coverage.add_argument("notes", type=Path)
    coverage.add_argument("--ed", type=Path, required=True)
    coverage.add_argument("--icu", type=Path, required=True)
    coverage.add_argument(
        "--extended", action="store_true", help="Scansiona anche i CSV di laboratorio ed eMAR."
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
    if args.command == "add-clinical-candidates":
        from codice_tesi.backend.clinical_candidates import run_clinical_candidates

        async with Database(settings) as database:
            await run_clinical_candidates(database, args)
        return
    if args.command == "audit-mimic-candidates":
        audit_candidate_coverage(args.hosp, args.notes, args.ed, args.icu, extended=args.extended)
        return
    if args.command == "import-mimic":
        budget = args.max_input_tokens
        if budget is not None and (budget < 1 or not args.hosp_ed_only or args.rich_pilot):
            raise ValueError("--max-input-tokens richiede --hosp-ed-only e l'estrazione compatta.")
        loaded = load_mimic(
            args.hosp,
            args.notes,
            limit=args.limit,
            seed=args.seed,
            ed=args.ed,
            icu=args.icu,
            min_discharge_cases=args.min_discharge_cases,
            min_complete_cases=args.min_complete_cases,
            rich_pilot=args.rich_pilot,
            hosp_ed_only=args.hosp_ed_only,
            candidate_batches=budget is not None,
        )
        if budget is not None:
            contexts, sources, references = select_mimic_within_budget(
                loaded, limit=args.limit, budget=budget
            )
        else:
            contexts, sources, references = loaded
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
        elif args.command == "audit-mimic":
            if not 1 <= args.expect <= MAX_CASES:
                raise ValueError(f"Usare da 1 a {MAX_CASES} ricoveri attesi.")
            await audit_mimic(database, args.hosp, args.expect)
        elif args.command == "profile-mimic":
            await profile_mimic(database)
        elif args.command == "compare-mimic":
            await compare_mimic(database, args.draft, args.destination)
            print(f"Confronto salvato: {args.destination}")


def select_mimic_within_budget(batches, *, limit, budget):
    from collections import Counter, defaultdict

    from codice_tesi.config import GEMMA_MODEL, QWEN_MODEL, ModelConfig
    from codice_tesi.discharge.config import ContextConfig
    from codice_tesi.discharge.context import prepare_request
    from codice_tesi.discharge.models import CompositionPacket
    from codice_tesi.discharge.template import load_template

    # Gemma e Mistral condividono la stima prudenziale in byte UTF-8.
    models = [
        ModelConfig(model=name, thinking=True, tools_enabled=True)
        for name in (QWEN_MODEL, GEMMA_MODEL)
    ]
    template = load_template()
    quota = limit // 2
    eligible = {False: set(), True: set()}
    candidates = []
    rejected = Counter()
    for contexts, sources, references in batches:
        by_context = defaultdict(list)
        for source in sources:
            by_context[source.context_id].append(source)
        for context in contexts:
            evidence = by_context[context.id]
            has_ed = any(source.origin == "ed/edstays" for source in evidence)
            patients = eligible[has_ed]
            if context.patient_id in patients or len(patients) == limit:
                continue
            packet = CompositionPacket(context=context, sources=tuple(evidence), template=template)
            estimate = max(
                prepare_request(
                    packet, ContextConfig(), output_tokens=model.max_tokens, model_config=model
                ).input_tokens_estimate
                for model in models
            )
            if estimate > budget:
                rejected[has_ed] += 1
                continue
            patients.add(context.patient_id)
            candidates.append((context, evidence, references[context.id], has_ed, estimate))
            if (
                all(len(group) >= quota for group in eligible.values())
                and len(eligible[False] | eligible[True]) >= limit
            ):
                break
        else:
            continue
        break
    else:
        raise ValueError(
            f"Entro il budget {budget}: {min(len(eligible[False]), quota)}/{quota} HOSP e "
            f"{min(len(eligible[True]), quota)}/{quota} HOSP + ED. Campione insufficiente "
            "dopo aver esaurito la graduatoria. "
            f"Oltre soglia: {rejected[False]} HOSP e {rejected[True]} HOSP + ED."
        )

    selected, selected_sources, estimates = [], [], []
    selected_references = {}
    used, counts = set(), Counter()
    for context, evidence, notes, has_ed, estimate in candidates:
        patient = context.patient_id
        other_patients = eligible[not has_ed] - used
        if (
            counts[has_ed] == quota
            or patient in used
            or (patient in other_patients and len(other_patients) == quota - counts[not has_ed])
        ):
            continue
        used.add(patient)
        counts[has_ed] += 1
        label = "Caso Hosp e ED" if has_ed else "Caso Hosp"
        selected.append(context.model_copy(update={"display_name": f"{label} {counts[has_ed]}"}))
        selected_sources.extend(evidence)
        selected_references[context.id] = notes
        estimates.append(estimate)
    digest = hashlib.sha256(
        json.dumps(
            [
                [
                    context.model_dump(exclude={"dataset_version"}, mode="json")
                    for context in selected
                ],
                [(source.id, source.sha256) for source in selected_sources],
                selected_references,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    print(
        f"Pacchetti entro budget, senza inferenza: {limit}; "
        f"{min(estimates)}–{max(estimates)} unità di input prudenziali "
        f"(tetto {budget}, scartati {sum(rejected.values())})."
    )
    return (
        [context.model_copy(update={"dataset_version": digest}) for context in selected],
        [source.model_copy(update={"version": digest}) for source in selected_sources],
        selected_references,
    )


def validate_mimic_requests(contexts, sources) -> None:
    from codice_tesi.config import ModelConfig, Settings
    from codice_tesi.discharge.config import ContextConfig
    from codice_tesi.discharge.context import prepare_request
    from codice_tesi.discharge.models import CompositionPacket
    from codice_tesi.discharge.template import load_template

    model = ModelConfig(model=Settings().model, thinking=False)
    if any(context.expanded_sources for context in contexts):
        print("Preparazione e stima dei pacchetti completi.", flush=True)
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
        f"Pacchetti completi preparati senza inferenza: {len(estimates)}, "
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
