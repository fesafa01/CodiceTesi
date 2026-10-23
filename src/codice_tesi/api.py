"""Accesso all'API OpenAI-compatible del DGX tramite Tailscale.

GET /v1/models: elenco dei modelli con client.models.list().
POST /v1/chat/completions: generazione tramite l'adattatore OpenAIChatModel.
PydanticAI invia una sola richiesta per la stesura, con reasoning opzionale.
"""

from contextlib import asynccontextmanager
from time import monotonic

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIStreamedResponse
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from codice_tesi.config import ModelConfig, Settings
from codice_tesi.stream_metrics import active_observation


class MeasuredStreamedResponse(OpenAIStreamedResponse):
    def _map_usage(self, response):
        if observation := active_observation.get():
            observation.observe(response)
        return super()._map_usage(response)


class MeasuredChatModel(OpenAIChatModel):
    @property
    def _streamed_response_cls(self):
        return MeasuredStreamedResponse

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs):
        observation = active_observation.get()
        if observation is not None:
            observation.start()
        try:
            async with super().request_stream(*args, **kwargs) as response:
                yield response
        finally:
            if observation is not None:
                observation.ended = monotonic()


def create_client(settings: Settings, *, streaming: bool = False) -> AsyncOpenAI:
    """Anche lo stream scade se il server non invia dati per settings.timeout secondi."""
    return AsyncOpenAI(
        base_url=str(settings.base_url),
        api_key=settings.api_key.get_secret_value(),
        timeout=settings.timeout,
        max_retries=0,
    )


async def list_models(settings: Settings) -> list[str]:
    """GET /v1/models: restituisce gli ID, chiudendo il client dopo la richiesta."""
    async with create_client(settings) as client:
        response = await client.models.list()
        return [model.id for model in response.data]


def create_model(config: ModelConfig, client: AsyncOpenAI) -> OpenAIChatModel:
    """Adattatore per i POST /v1/chat/completions del generatore della lettera."""
    return MeasuredChatModel(
        config.model,
        provider=OpenAIProvider(openai_client=client),
        profile=OpenAIModelProfile(
            openai_supports_strict_tool_definition=False,
            openai_chat_supports_max_completion_tokens=False,
        ),
    )
