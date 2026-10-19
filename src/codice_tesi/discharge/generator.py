"""Agente in streaming: una sola richiesta per l’intera bozza Campus."""

from collections.abc import AsyncIterator

from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from codice_tesi import api
from codice_tesi.config import ModelConfig
from codice_tesi.discharge.context import PreparedRequest


class LLMDraftGenerator:
    """Una richiesta PydanticAI per lettera, senza tool o retry automatici."""

    def __init__(self, config: ModelConfig, client: AsyncOpenAI):
        self.model = api.create_model(config, client)
        self.settings = config.request_settings()
        self.settings["tool_choice"] = "none"
        self.usage: dict = {}

    async def stream(self, request: PreparedRequest) -> AsyncIterator[str]:
        self.usage = {}
        agent = Agent(
            self.model,
            instructions=request.instructions,
            model_settings=self.settings,
            retries=0,
        )
        async with agent.run_stream(
            request.prompt, usage_limits=UsageLimits(request_limit=1, tool_calls_limit=0)
        ) as response:
            async for text in response.stream_text(delta=True, debounce_by=None):
                yield text
            usage = response.usage
            self.usage = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "finish_reason": response.response.finish_reason,
            }
            if response.response.finish_reason != "stop":
                raise ValueError(
                    "Il modello non ha concluso normalmente la lettera "
                    f"({response.response.finish_reason}); testo conservato come parziale."
                )
