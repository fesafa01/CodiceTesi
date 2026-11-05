"""Accesso all'API OpenAI-compatible del DGX tramite Tailscale.

GET /v1/models: elenco dei modelli con client.models.list().
POST /v1/chat/completions: generazione tramite l'adattatore OpenAIChatModel.
Il generatore gestisce stesura ed elaborazioni documentali opzionali sullo snapshot della degenza.
"""

from contextlib import asynccontextmanager, nullcontext
from time import monotonic

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIStreamedResponse
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from codice_tesi.config import OLLAMA_MODELS, ModelConfig, Settings
from codice_tesi.observability.stream_metrics import RunStreamObservation, active_observation


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
    async def request_stream(
        self, messages, model_settings, model_request_parameters, run_context=None
    ):
        run_observation = active_observation.get()
        observation = run_observation
        token = None
        if isinstance(observation, RunStreamObservation):
            settings = model_settings or {}
            parts = [part for message in messages for part in message.parts]
            metadata = {
                "tools_available": len(model_request_parameters.function_tools),
                "history_messages": len(messages),
                "user_parts": sum(p.part_kind == "user-prompt" for p in parts),
                "tool_return_parts": sum(p.part_kind == "tool-return" for p in parts),
                "retry_parts": sum(p.part_kind == "retry-prompt" for p in parts),
                "assistant_tool_calls": sum(p.part_kind == "tool-call" for p in parts),
            }
            for field in ("max_tokens", "temperature", "top_p"):
                if type(settings.get(field)) in (int, float):
                    metadata[field] = settings[field]
            effort = settings.get("openai_reasoning_effort")
            if effort in {"none", "off", "minimal", "low", "medium", "high", "max", "xhigh"}:
                metadata["reasoning_effort"] = effort
            thinking = (settings.get("extra_body") or {}).get("thinking", {}).get("type")
            if thinking in {"enabled", "disabled"}:
                metadata["thinking"] = thinking == "enabled"
            elif self.model_name in OLLAMA_MODELS:
                metadata["thinking"] = effort != "none"
            observation = observation.begin(metadata)
            token = active_observation.set(observation)
        elif observation is not None:
            observation.start()
        context = (
            run_observation.trace_request(observation)
            if isinstance(run_observation, RunStreamObservation) and run_observation.trace_request
            else nullcontext()
        )
        try:
            with context:
                try:
                    async with super().request_stream(
                        messages, model_settings, model_request_parameters, run_context
                    ) as response:
                        yield response
                    if observation is not None:
                        observation.provider_complete = True
                finally:
                    if observation is not None:
                        observation.ended = monotonic()
        finally:
            if token is not None:
                active_observation.reset(token)


def create_client(settings: Settings) -> AsyncOpenAI:
    """Anche lo stream scade se il server non invia dati per settings.timeout secondi."""
    return AsyncOpenAI(
        base_url=str(settings.base_url),
        api_key=settings.api_key.get_secret_value(),
        timeout=settings.timeout,
        max_retries=0,
    )


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
