"""Agente PydanticAI collegato all'API del server esposto via Tailscale."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModelSettings

from codice_tesi import api
from codice_tesi.config import ModelConfig, Settings

INSTRUCTIONS = """Sei un assistente per il prototipo di una tesi sul supporto documentale clinico.
Rispondi in italiano, salvo diversa richiesta. Quando elabori documenti, usa soltanto le
informazioni fornite e segnala dati mancanti o contraddizioni senza inventare fatti.
Le bozze cliniche sono destinate alla revisione di un professionista.
"""


@asynccontextmanager
async def create_agent(
    config: ModelConfig | None = None,
    *,
    settings: Settings | None = None,
    client: AsyncOpenAI | None = None,
) -> AsyncIterator[Agent[None, str]]:
    """Apre l'agente e chiude il client HTTP all'uscita, anche in caso di errore."""
    settings = settings or Settings()
    config = config or ModelConfig()

    def request_settings(ctx: RunContext[None]) -> OpenAIChatModelSettings:
        values = config.request_settings()
        # Dopo il tool obbligatorio, consentire una risposta finale testuale.
        if values.get("tool_choice") == "required" and ctx.run_step > 1:
            values["tool_choice"] = "auto"
        return values

    async with client or api.create_client(settings) as connection:
        yield Agent(
            api.create_model(config, connection),
            deps_type=type(None),
            instructions=INSTRUCTIONS,
            model_settings=request_settings,
        )


async def reply(
    prompt: str, config: ModelConfig | None = None, *, settings: Settings | None = None
) -> AsyncGenerator[str]:
    """Restituisce frammenti di testo, oppure la risposta completa se stream=False."""
    config = config or ModelConfig()
    async with create_agent(config, settings=settings) as agent:
        if config.stream:
            async with agent.run_stream(prompt) as response:
                async for text in response.stream_text(delta=True, debounce_by=None):
                    yield text
        else:
            yield (await agent.run(prompt)).output
