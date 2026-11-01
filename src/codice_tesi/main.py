"""Entry point del prototipo con modello remoto."""

import argparse
import asyncio
import json
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    """Traduce gli argomenti CLI in configurazione e rende leggibili gli errori.

    La compilazione non accetta un prompt libero: delega il caso d'uso
    ad application.run_application. L'unica utility è l'elenco dei modelli.
    """
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    parser = argparse.ArgumentParser(description="Prototipo di compilazione della dimissione.")
    commands = parser.add_subparsers(dest="command")
    discharge = commands.add_parser("dimissione", help="Redige la bozza Campus in streaming.")
    selection = discharge.add_mutually_exclusive_group()
    selection.add_argument("--seed", type=int, help="Seed della selezione casuale del contesto.")
    selection.add_argument(
        "--contesto", help="Fissa il contesto già aperto, senza selezione casuale."
    )
    discharge.add_argument(
        "--thinking",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Attiva o disattiva il reasoning nella stessa richiesta che produce la bozza.",
    )
    discharge.add_argument("--model", help="ID del modello richiesto al server configurato.")
    discharge.add_argument(
        "--experiment-file",
        type=Path,
        help="JSON dei metadati dichiarati: esperimento, checkpoint, hardware e ripetizione.",
    )
    discharge.add_argument(
        "--telemetry",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Abilita/disabilita OTLP; configurazione in observability/.env o TELEMETRY_*.",
    )
    discharge.add_argument(
        "--hardware",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Raccoglie i sensori Spark; configurazione in observability/hardware.env.",
    )
    discharge.add_argument(
        "--dry-run",
        action="store_true",
        help="Salva la richiesta completa senza chiamare il modello.",
    )
    discharge.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/dimissioni"),
        help="Directory base; salva in <directory>/<contesto>/<esecuzione>/.",
    )
    discharge.add_argument(
        "--context-window",
        type=int,
        default=100000,
        help="Budget della richiesta in token stimati: note, campi Campus, output e margine.",
    )
    discharge.add_argument(
        "--backend-url",
        default=os.getenv("APP_BACKEND_URL", "http://127.0.0.1:8001"),
        help="URL del backend clinico; default APP_BACKEND_URL o http://127.0.0.1:8001.",
    )
    commands.add_parser("modelli", help="Elenca i modelli del server LLM.")
    registry = commands.add_parser("registro", help="Esporta il registro in CSV o JSONL.")
    registry.add_argument("--output-dir", type=Path, default=Path("output/dimissioni"))
    registry.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    registry.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["dimissione"])
    if args.command == "dimissione" and args.seed is not None and args.seed < 0:
        parser.error("il seed deve essere non negativo")

    from openai import APIConnectionError, APIStatusError, APITimeoutError
    from pydantic import ValidationError
    from pydantic_ai.exceptions import AgentRunError, ModelAPIError, UserError

    from codice_tesi import api
    from codice_tesi.config import ModelConfig, Settings

    try:
        if args.command == "registro":
            from codice_tesi.observability.execution import export_registry

            count = export_registry(args.output_dir, args.destination, format=args.format)
            print(f"Esportate {count} esecuzioni: {args.destination}")
            return
        if args.command == "dimissione":
            from codice_tesi.application import run_application
            from codice_tesi.discharge.config import ContextConfig
            from codice_tesi.observability.execution import ExperimentConfig
            from codice_tesi.observability.hardware import HardwareConfig
            from codice_tesi.observability.telemetry import TelemetryConfig

            config = ModelConfig()
            if args.thinking is not None:
                config.thinking = args.thinking
            if args.model is not None:
                config.model = args.model
            experiment = (
                ExperimentConfig.model_validate(json.loads(args.experiment_file.read_text()))
                if args.experiment_file
                else None
            )
            telemetry_config = TelemetryConfig()
            if args.telemetry is not None:
                telemetry_config.enabled = args.telemetry
            hardware_config = HardwareConfig()
            if args.hardware is not None:
                hardware_config.enabled = args.hardware
            asyncio.run(
                run_application(
                    backend_url=args.backend_url,
                    output_dir=args.output_dir,
                    seed=args.seed,
                    context_id=args.contesto,
                    dry_run=args.dry_run,
                    config=config,
                    context_config=ContextConfig(window_tokens=args.context_window),
                    experiment=experiment,
                    telemetry_config=telemetry_config,
                    hardware_config=hardware_config,
                )
            )
            return
        for model in asyncio.run(api.list_models(Settings())):
            print(model)
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(map(str, error["loc"])) or "contratto" for error in exc.errors()
        )
        if args.command == "dimissione":
            parser.exit(2, f"Dati o configurazione non validi ({fields}).\n")
        parser.exit(2, f"Configurazione non valida: controlla {fields} in ModelConfig o .env.\n")
    except (APIConnectionError, ModelAPIError) as exc:
        cause = exc.__cause__ if isinstance(exc, ModelAPIError) else exc
        if isinstance(cause, APITimeoutError):
            parser.exit(
                1,
                "Timeout dello Spark: nessun dato ricevuto entro LLM_TIMEOUT. "
                "Verifica richieste ancora attive e stato del server.\n",
            )
        if isinstance(cause, APIConnectionError):
            parser.exit(1, "Connessione fallita: verifica Tailscale, LLM_BASE_URL e LLM_TIMEOUT.\n")
        parser.exit(1, f"Errore del modello: {exc}\n")
    except (APIStatusError, AgentRunError, UserError) as exc:
        parser.exit(1, f"Errore del modello: {exc}\n")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Compilazione non completata: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130)
