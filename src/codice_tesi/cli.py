"""Interfaccia a riga di comando per provare il modello remoto."""

import argparse
import asyncio
import os
import sys

from openai import APIConnectionError, APIStatusError
from pydantic import ValidationError
from pydantic_ai.exceptions import AgentRunError, UserError

from codice_tesi import api
from codice_tesi.agent import reply
from codice_tesi.config import ModelConfig, Settings


async def run(
    prompt: str | None, *, list_models: bool, settings: Settings, config: ModelConfig
) -> None:
    if list_models:
        for model in await api.list_models(settings):
            print(model)
    elif prompt is not None:
        async for text in reply(prompt, config, settings=settings):
            print(text, end="", flush=True)
        print()


def main() -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    parser = argparse.ArgumentParser(description="Agente della tesi con modello via Tailscale.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("prompt", nargs="?", help="Richiesta da inviare al modello.")
    action.add_argument("--list-models", action="store_true", help="Elenca i modelli del server.")
    parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Abilita lo streaming.",
    )
    args = parser.parse_args()
    if args.prompt is not None and not args.prompt.strip():
        parser.error("il prompt non può essere vuoto")

    try:
        settings = Settings()
        config = ModelConfig()
        if args.stream is not None:
            config.stream = args.stream
        asyncio.run(
            run(args.prompt, list_models=args.list_models, settings=settings, config=config)
        )
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        parser.exit(2, f"Configurazione non valida: controlla {fields} in ModelConfig o .env.\n")
    except APIConnectionError:
        parser.exit(1, "Connessione fallita: verifica Tailscale, LLM_BASE_URL e LLM_TIMEOUT.\n")
    except (APIStatusError, AgentRunError, UserError) as exc:
        parser.exit(1, f"Errore del modello: {exc}\n")
    except KeyboardInterrupt:
        sys.exit(130)
