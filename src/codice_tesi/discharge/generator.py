"""Stesura con reasoning ed elaborazioni documentali opzionali, contatori e limiti espliciti."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from time import monotonic

from openai import AsyncOpenAI
from pydantic_ai import Agent, AgentRunResultEvent, RunContext, Tool
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.usage import RunUsage, UsageLimits

from codice_tesi import api
from codice_tesi.config import OLLAMA_MODELS, ModelConfig
from codice_tesi.discharge.context import PreparedRequest
from codice_tesi.discharge.limits import (
    EvidenceReadError,
    FinalPhaseToolCallError,
    ModelInactivityError,
    ToolCallValidationError,
    execution_deadline,
)
from codice_tesi.discharge.token_count import estimate_input_tokens
from codice_tesi.discharge.tools import DOCUMENTARY_TOOLS, TOOL_NAMES, DocumentaryTools
from codice_tesi.observability.model_spans import request_span
from codice_tesi.observability.stream_metrics import RunStreamObservation, active_observation
from codice_tesi.observability.telemetry import Telemetry
from codice_tesi.observability.tool_spans import trace_rejected_tool


@asynccontextmanager
async def _first_text_deadline(seconds: float):
    """Limita il silenzio prima della lettera; il reasoning rinnova l'attesa."""
    timer = asyncio.timeout(seconds)
    try:
        async with timer:
            yield timer
    except TimeoutError:
        if timer.expired():
            raise ModelInactivityError(seconds) from None
        raise


class LLMDraftGenerator:
    """Stesura singola o approfondimento con tool locali e budget espliciti."""

    def __init__(self, config: ModelConfig, client: AsyncOpenAI):
        self.model = api.create_model(config, client)
        self.settings = config.request_settings()
        self.config = config
        if not config.tools_enabled:
            self.settings["tool_choice"] = "none"
        self.first_text_timeout = config.first_text_timeout
        self.usage: dict = {}
        self.progress: dict = {}
        self.partial_text = ""
        self.tools: DocumentaryTools | None = None
        self.telemetry: Telemetry | None = None
        self.partial_callback: Callable[[str, bool], None] | None = None

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
        self.tools = DocumentaryTools(
            request.catalog,
            request.facts,
            self.config.max_tool_calls,
            self.config.max_tool_result_bytes,
            telemetry=self.telemetry,
        )
        step_budgets = []
        tool_attempts = []
        known_tools = TOOL_NAMES
        step_plans = {}
        final_control = (
            "# Fase finale obbligatoria\n"
            "Scrivi adesso la bozza completa. La prossima azione deve essere la risposta "
            "finale JSON, non una chiamata a strumenti. Cita i passaggi ricevuti nel "
            "catalogo iniziale o attraverso i risultati degli strumenti. "
            "Non servono altre letture o verifiche. Gli strumenti sono chiusi. "
            'Inizia direttamente con {"sections":[ e termina con ]}, includendo le 17 '
            "sezioni nell'ordine previsto, senza premesse o Markdown. "
            "Per i dati assenti usa la formula del template, senza inventare informazioni."
        )

        def budget_for_step(ctx):
            if ctx.run_step in step_plans:
                return step_plans[ctx.run_step]
            # Stima il contesto reinviato per il registro e per eventuali limiti espliciti.
            history = ModelMessagesTypeAdapter.dump_json(
                ctx.messages[1:], exclude={"__all__": {"instructions"}}
            ).decode()
            estimate = estimate_input_tokens(
                request.instructions, request.prompt + "\n" + history, self.config
            ).tokens
            token_limit = self.config.total_tokens_limit
            remaining = (
                token_limit - ctx.usage.total_tokens - estimate - 8192
                if token_limit is not None
                else None
            )
            # Un tetto aggregato esplicito lascia spazio alla risposta finale.
            final_reason = None
            if ctx.usage.requests + 1 >= self.config.max_requests:
                final_reason = "request_limit"
            elif (
                remaining is not None
                and remaining < estimate + min(self.config.max_tokens, 8192) + 8192
            ):
                final_reason = "token_budget"
            if final_reason:
                estimate = estimate_input_tokens(
                    request.instructions,
                    request.prompt + "\n" + history + "\n" + final_control,
                    self.config,
                ).tokens
                if token_limit is not None:
                    remaining = token_limit - ctx.usage.total_tokens - estimate - 8192
            if request.window_tokens is not None and (
                estimate + self.config.max_tokens + request.safety_margin_tokens + 8192
                > request.window_tokens
            ):
                raise UsageLimitExceeded(
                    "Storia agentica oltre il budget di contesto; nessuna fonte troncata."
                )
            if remaining is not None and remaining <= 0:
                raise UsageLimitExceeded(
                    "Budget aggregato di token esaurito prima della nuova richiesta."
                )
            step_plans[ctx.run_step] = estimate, remaining, final_reason
            return step_plans[ctx.run_step]

        async def history_for_step(ctx: RunContext[DocumentaryTools], messages):
            if budget_for_step(ctx)[2]:
                # Conserva system, fonti, ID delle tool call e risultati esatti.
                # Il controllo finale è un nuovo messaggio in coda: nessun reset
                # della storia e nessuna ricostruzione delle risposte dei tool.
                return [
                    *messages,
                    ModelRequest.user_text_prompt(final_control, instructions=request.instructions),
                ]
            return messages

        def settings_for_step(ctx):
            estimate, remaining, final_reason = budget_for_step(ctx)
            settings = dict(self.settings)
            if remaining is not None:
                settings["max_tokens"] = min(self.config.max_tokens, remaining)
            if final_reason:
                self.progress["fase"] = "redazione_finale"
            elif self.config.model in OLLAMA_MODELS:
                # La grammatica JSON di Ollama interferisce con la chiamata ai tool.
                settings.pop("extra_body", None)
            step_budgets.append(
                {
                    "input_tokens_estimate": estimate,
                    "max_output_tokens": settings["max_tokens"],
                    "final_only_reason": final_reason,
                    "history_mode": "final_append" if final_reason else "conversation",
                    "prefix_strategy": "append_only",
                }
            )
            return settings

        agent = Agent(
            self.model,
            instructions=request.instructions,
            model_settings=settings_for_step if self.config.tools_enabled else self.settings,
            # Una correzione degli argomenti, senza retry di trasporto o dell'output.
            # La nuova richiesta rimane soggetta ai budget complessivi del run.
            retries={
                "tools": self.config.tool_validation_retries if self.config.tools_enabled else 0,
                "output": 0,
            },
            tools=[Tool(fn) for fn in DOCUMENTARY_TOOLS] if self.config.tools_enabled else (),
            deps_type=DocumentaryTools,
            capabilities=[ProcessHistory(history_for_step)] if self.config.tools_enabled else (),
            tool_timeout=10,
        )

        def trace_request(sample):
            self.tools.request_index = sample.request_metadata["index"]
            plan = (
                step_budgets[-1]
                if step_budgets
                else {
                    "history_mode": "single_request",
                    "input_tokens_estimate": request.input_tokens_estimate,
                }
            )
            self.tools.final_only = plan.get("final_only_reason") is not None
            sample.request_metadata.update(
                {
                    k: plan[k]
                    for k in (
                        "history_mode",
                        "final_only_reason",
                        "input_tokens_estimate",
                        "prefix_strategy",
                    )
                    if plan.get(k) is not None
                }
            )
            sample.request_metadata["tool_execution_enabled"] = not self.tools.final_only
            return request_span(self.telemetry, sample, self.config.model)

        observation = RunStreamObservation(trace_request=trace_request)
        token = active_observation.set(observation)
        completed = False
        events = None
        response_text = ""
        response_started_text = False
        try:
            async with (
                execution_deadline(self.config.total_timeout),
                _first_text_deadline(self.first_text_timeout) as first_text_timer,
                agent.run_stream_events(
                    request.prompt,
                    usage=usage,
                    deps=self.tools,
                    usage_limits=UsageLimits(
                        request_limit=self.config.max_requests if self.config.tools_enabled else 1,
                        tool_calls_limit=self.config.max_tool_calls
                        if self.config.tools_enabled
                        else 0,
                        total_tokens_limit=self.config.total_tokens_limit,
                    ),
                ) as events,
            ):
                async for event in events:
                    text = ""
                    thinking = ""
                    if isinstance(event, PartStartEvent):
                        if self.config.tools_enabled and event.index == 0:
                            response_text = ""
                            response_started_text = False
                            if self.partial_callback:
                                self.partial_callback("", True)
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
                        if self.config.tools_enabled:
                            # Un preambolo prima di un tool non è parte della lettera.
                            response_text = event.result.output
                    elif isinstance(event, FunctionToolCallEvent):
                        if len(tool_attempts) >= self.config.max_tool_calls:
                            raise UsageLimitExceeded(
                                "Limite dei tentativi di chiamata ai tool raggiunto."
                            )
                        name = event.part.tool_name
                        safe_name = name if name in known_tools else "tool_sconosciuto"
                        tool_attempts.append({"tool": safe_name, "args_valid": event.args_valid})
                        rejected = event.args_valid is False
                        self.progress["fase"] = "correzione_tool" if rejected else "lettura_fonti"
                        self.progress["tool_calls"] = len(tool_attempts)
                        self.progress["tool_rejections"] = sum(
                            call["args_valid"] is False for call in tool_attempts
                        )
                        if rejected:
                            trace_rejected_tool(
                                self.telemetry,
                                safe_name,
                                len(tool_attempts),
                                request_index=self.tools.request_index,
                            )
                        if rejected and self.tools.final_only:
                            raise FinalPhaseToolCallError(safe_name)
                        first_text_timer.reschedule(
                            asyncio.get_running_loop().time() + self.first_text_timeout
                        )
                    elif isinstance(event, FunctionToolResultEvent):
                        self.progress["fase"] = (
                            "correzione_tool"
                            if isinstance(event.part, RetryPromptPart)
                            else "attesa_risposta"
                        )
                    if text or thinking:
                        elapsed = round(monotonic() - started, 2)
                        self.progress.setdefault("primo_evento_secondi", elapsed)
                        if thinking:
                            self.progress["fase"] = "ragionamento"
                            self.progress["caratteri_ragionamento"] += len(thinking)
                            if thinking.strip() and not response_started_text:
                                first_text_timer.reschedule(
                                    asyncio.get_running_loop().time() + self.first_text_timeout
                                )
                        if text:
                            if not response_started_text and text.strip():
                                first_text_timer.reschedule(None)
                                self.progress.setdefault("primo_testo_secondi", elapsed)
                                response_started_text = True
                            self.progress["fase"] = "redazione"
                            self.progress["caratteri_testo"] += len(text)
                            if self.config.tools_enabled:
                                response_text += text
                                if self.partial_callback:
                                    self.partial_callback(text, False)
                            else:
                                yield text
                completed = True
            if self.config.tools_enabled and finish_reason == "stop":
                yield response_text
        except UnexpectedModelBehavior as exc:
            if tool_attempts and tool_attempts[-1]["args_valid"] is False:
                raise ToolCallValidationError(tool_attempts[-1]["tool"]) from exc
            if self.tools.calls and self.tools.calls[-1]["status"] == "failed":
                raise EvidenceReadError(
                    self.tools.calls[-1].get("error_code", "read_failed")
                ) from exc
            raise
        finally:
            active_observation.reset(token)
            # Include errori prima del primo chunk e cancellazioni del consumer.
            messages = events.all_messages() if events is not None and usage.requests else []
            for message in reversed(messages):
                if isinstance(message, ModelResponse):
                    finish_reason = message.finish_reason
                    break
            self.progress["durata_secondi"] = round(monotonic() - started, 6)
            self.partial_text = response_text if self.config.tools_enabled else ""
            metrics = observation.snapshot(complete=completed)
            metrics["finish_reason"] = finish_reason
            self.usage = {
                "input_tokens": metrics["input_tokens"],
                "output_tokens": metrics["output_tokens"],
                "finish_reason": finish_reason,
                "stream": dict(self.progress),
                "metrics": metrics,
            }
            if self.config.tools_enabled:
                self.usage["tools"] = self.tools.audit()
                self.usage["tools"]["attempts"] = tool_attempts
                self.usage["step_budgets"] = step_budgets
        if finish_reason != "stop":
            raise ValueError(
                "Il modello non ha concluso normalmente la lettera "
                f"({finish_reason}); testo conservato come parziale."
            )
