"""Interfaccia a riga di comando per provare il modello remoto."""

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
