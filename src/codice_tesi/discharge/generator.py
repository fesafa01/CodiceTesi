"""Stesura in una richiesta, con reasoning opzionale, contatori e limiti espliciti."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic

from openai import AsyncOpenAI
from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.messages import (
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.usage import RunUsage, UsageLimits

from codice_tesi import api
from codice_tesi.config import ModelConfig
from codice_tesi.discharge.context import PreparedRequest
from codice_tesi.stream_metrics import StreamObservation, active_observation


@asynccontextmanager
async def _first_text_deadline(seconds: float):
    """Il reasoning non può consumare indefinitamente il tempo prima della lettera."""
    timer = asyncio.timeout(seconds)
    try:
        async with timer:
            yield timer
    except TimeoutError:
        if timer.expired():
            raise ValueError(
                f"Nessun testo della lettera entro {seconds:g} secondi. "
                "Limite di attesa iniziale/ragionamento raggiunto; "
                "richiesta interrotta senza retry. Contatori conservati in esito.json."
            ) from None
        raise


class LLMDraftGenerator:
    """Una richiesta PydanticAI per lettera, senza tool o retry automatici."""

    def __init__(self, config: ModelConfig, client: AsyncOpenAI):
        self.model = api.create_model(config, client)
        self.settings = config.request_settings()
        self.settings["tool_choice"] = "none"
        self.first_text_timeout = config.first_text_timeout
        self.usage: dict = {}
        self.progress: dict = {}

    async def stream(self, request: PreparedRequest) -> AsyncIterator[str]:
        self.usage = {}
        started = monotonic()
        self.progress = {
            "fase": "attesa_risposta",
            "caratteri_testo": 0,
            "caratteri_ragionamento": 0,
        }
        usage = RunUsage()
        finish_reason = None
        agent = Agent(
            self.model,
            instructions=request.instructions,
            model_settings=self.settings,
            retries=0,
        )
        observation = StreamObservation()
        token = active_observation.set(observation)
        completed = False
        events = None
        try:
            async with (
                _first_text_deadline(self.first_text_timeout) as first_text_timer,
                agent.run_stream_events(
                    request.prompt,
                    usage=usage,
                    usage_limits=UsageLimits(request_limit=1, tool_calls_limit=0),
                ) as events,
            ):
                async for event in events:
                    text = ""
                    thinking = ""
                    if isinstance(event, PartStartEvent):
                        if isinstance(event.part, TextPart):
                            text = event.part.content
                        elif isinstance(event.part, ThinkingPart):
                            thinking = event.part.content
                    elif isinstance(event, PartDeltaEvent):
                        if isinstance(event.delta, TextPartDelta):
                            text = event.delta.content_delta
                        elif isinstance(event.delta, ThinkingPartDelta):
                            thinking = event.delta.content_delta or ""
                    elif isinstance(event, AgentRunResultEvent):
                        finish_reason = event.result.response.finish_reason
                    if text or thinking:
                        elapsed = round(monotonic() - started, 2)
                        self.progress.setdefault("primo_evento_secondi", elapsed)
                        if thinking:
                            self.progress["fase"] = "ragionamento"
                            self.progress["caratteri_ragionamento"] += len(thinking)
                        if text:
                            if "primo_testo_secondi" not in self.progress and text.strip():
                                first_text_timer.reschedule(None)
                                self.progress["primo_testo_secondi"] = elapsed
                            self.progress["fase"] = "redazione"
                            self.progress["caratteri_testo"] += len(text)
                            yield text
                completed = True
        finally:
            active_observation.reset(token)
            # Include errori prima del primo chunk e cancellazioni del consumer.
            messages = events.all_messages() if events is not None and usage.requests else []
            for message in reversed(messages):
                if isinstance(message, ModelResponse):
                    finish_reason = message.finish_reason
                    break
            self.progress["durata_secondi"] = round(monotonic() - started, 6)
            metrics = observation.snapshot(complete=completed)
            metrics["finish_reason"] = finish_reason
            self.usage = {
                "input_tokens": metrics["input_tokens"],
                "output_tokens": metrics["output_tokens"],
                "finish_reason": finish_reason,
                "stream": dict(self.progress),
                "metrics": metrics,
            }
        if finish_reason != "stop":
            raise ValueError(
                "Il modello non ha concluso normalmente la lettera "
                f"({finish_reason}); testo conservato come parziale."
            )
