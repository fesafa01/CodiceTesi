"""Agente PydanticAI collegato all'API del server esposto via Tailscale."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from codice_tesi.config import Settings

INSTRUCTIONS = """Sei un assistente per il prototipo di una tesi sul supporto documentale clinico.
Rispondi in italiano, salvo diversa richiesta. Quando elabori documenti, usa soltanto le
informazioni fornite e segnala dati mancanti o contraddizioni senza inventare fatti.
Le bozze cliniche sono destinate alla revisione di un professionista.
"""


def create_client(settings: Settings) -> AsyncOpenAI:
    """Crea un client diretto al solo endpoint configurato, senza retry automatici."""
    return AsyncOpenAI(
        base_url=str(settings.base_url),
        api_key=settings.api_key.get_secret_value(),
        timeout=settings.timeout,
        max_retries=0,
    )


@asynccontextmanager
async def create_agent(
    settings: Settings | None = None, *, client: AsyncOpenAI | None = None
) -> AsyncIterator[Agent[None, str]]:
    """Apre l'agente e chiude il client HTTP all'uscita, anche in caso di errore."""
    settings = settings or Settings()

    def request_settings(ctx: RunContext[None]) -> OpenAIChatModelSettings:
        values = settings.request_settings()
        # Dopo il tool obbligatorio, consentire una risposta finale testuale.
        if values.get("tool_choice") == "required" and ctx.run_step > 1:
            values["tool_choice"] = "auto"
        return values

    async with client or create_client(settings) as connection:
        model = OpenAIChatModel(
            settings.model,
            provider=OpenAIProvider(openai_client=connection),
            profile=OpenAIModelProfile(
                openai_supports_strict_tool_definition=False,
                openai_chat_supports_max_completion_tokens=False,
            ),
        )
        yield Agent(
            model,
            deps_type=type(None),
            instructions=INSTRUCTIONS,
            model_settings=request_settings,
        )
