"""Entry point del prototipo con modello remoto."""

import argparse
import asyncio
import os


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    parser = argparse.ArgumentParser(description="Prototipo della tesi: modello remoto.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("modelli", help="Elenca i modelli del server LLM.")
    agent = commands.add_parser("agente", help="Invia un prompt al modello remoto.")
    agent.add_argument("prompt", help="Richiesta da inviare al modello.")
    agent.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return

    from openai import APIConnectionError, APIStatusError
    from pydantic import ValidationError
    from pydantic_ai.exceptions import AgentRunError, UserError

    from codice_tesi.cli import run
    from codice_tesi.config import ModelConfig, Settings

    prompt = getattr(args, "prompt", None)
    if prompt is not None and not prompt.strip():
        parser.error("il prompt non può essere vuoto")
    try:
        settings = Settings()
        config = ModelConfig()
        if getattr(args, "stream", None) is not None:
            config.stream = args.stream
        asyncio.run(
            run(prompt, list_models=args.command == "modelli", settings=settings, config=config)
        )
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        parser.exit(2, f"Configurazione non valida: controlla {fields} in ModelConfig o .env.\n")
    except APIConnectionError:
        parser.exit(1, "Connessione fallita: verifica Tailscale, LLM_BASE_URL e LLM_TIMEOUT.\n")
    except (APIStatusError, AgentRunError, UserError) as exc:
        parser.exit(1, f"Errore del modello: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130)
