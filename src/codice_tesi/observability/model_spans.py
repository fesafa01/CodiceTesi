"""Tracce distinte per ciclo agente e singola richiesta al provider.

Solo i figli LLM hanno i contatori standard Phoenix. I totali sul padre AGENT
usano attributi applicativi, così non vengono sommati una seconda volta.
"""

import json
from contextlib import contextmanager

from opentelemetry.trace import SpanKind

_FINISH_REASONS = {"stop", "length", "tool_calls", "function_call", "content_filter", "error"}


def _finish_reason(value):
    return value if value in _FINISH_REASONS else "unknown"


def request_attributes(model, sample):
    request = sample.get("request", {})
    attributes = {
        "openinference.span.kind": "LLM",
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "local_openai_compatible",
        "gen_ai.request.model": model,
        "llm.model_name": sample.get("response_model") or model,
        "tesi.measurement.boundary": "provider_request_stream_client",
        "tesi.usage.available": sample.get("usage_available", False),
        "tesi.usage.complete": sample.get("usage_complete", False),
        "tesi.request.stream_complete": sample.get("stream_complete", False),
        "tesi.request.tool_calls_requested": sample.get("tool_calls_requested", 0),
        "tesi.request.cache.source": "provider_stream_usage",
    }
    fields = {
        "input_tokens": ("gen_ai.usage.input_tokens", "llm.token_count.prompt"),
        "output_tokens": ("gen_ai.usage.output_tokens", "llm.token_count.completion"),
        "total_tokens": ("llm.token_count.total",),
        "reasoning_tokens": ("gen_ai.usage.reasoning.output_tokens",),
        "visible_output_tokens": ("tesi.request.usage.non_reasoning_output_tokens",),
        "cached_input_tokens": ("gen_ai.usage.cache_read.input_tokens",),
        "uncached_input_tokens": ("tesi.request.usage.uncached_input_tokens",),
        "response_model": ("gen_ai.response.model",),
        "request_duration_seconds": ("tesi.request.timing.duration_seconds",),
        "time_to_first_chunk_seconds": ("gen_ai.response.time_to_first_chunk",),
        "time_to_first_text_seconds": ("tesi.request.timing.first_text_seconds",),
        "time_to_first_reasoning_seconds": ("tesi.request.timing.first_reasoning_seconds",),
        "time_to_first_tool_call_seconds": ("tesi.request.timing.first_tool_call_seconds",),
        "output_tokens_per_request_second": ("tesi.request.output_tokens_per_second",),
        "server_decode_tokens_per_second": ("tesi.request.server.decode_tokens_per_second",),
        "content_chunks_received": ("tesi.request.stream.content_chunks",),
        "chunks_received": ("tesi.request.stream.chunks",),
    }
    for field, names in fields.items():
        if sample.get(field) is not None:
            for name in names:
                attributes[name] = sample[field]
    if sample.get("finish_reason") is not None:
        attributes["gen_ai.response.finish_reasons"] = [_finish_reason(sample["finish_reason"])]
    if sample.get("usage_issues"):
        attributes["tesi.usage.issues"] = sample["usage_issues"]
    cached = sample.get("cached_input_tokens")
    attributes["tesi.request.cache.status"] = (
        "inconsistent"
        if "cached_exceeds_input" in sample.get("usage_issues", [])
        else "unreported"
        if cached is None
        else "reported_zero"
        if cached == 0
        else "reported_reuse"
    )
    request_fields = {
        "index": "index",
        "max_tokens": "budget.max_output_tokens",
        "input_tokens_estimate": "context.input_tokens_estimate",
        "history_mode": "context.history_mode",
        "prefix_strategy": "context.prefix_strategy",
        "tool_execution_enabled": "tools.execution_enabled",
        "final_only_reason": "budget.final_only_reason",
        "history_messages": "context.history_message_count",
        "user_parts": "context.user_part_count",
        "tool_return_parts": "context.tool_result_part_count",
        "retry_parts": "context.retry_part_count",
        "assistant_tool_calls": "context.assistant_tool_call_count",
        "tools_available": "tools.definition_count",
        "temperature": "sampling.temperature",
        "top_p": "sampling.top_p",
        "thinking": "reasoning.enabled",
        "reasoning_effort": "reasoning.effort",
    }
    for field, target in request_fields.items():
        if request.get(field) is not None:
            attributes["tesi.request." + target] = request[field]
    attributes["tesi.request.phase"] = (
        "tool_selection"
        if sample.get("finish_reason") == "tool_calls"
        else "final_response"
        if sample.get("finish_reason") == "stop"
        else "incomplete"
    )
    if request.get("max_tokens") is not None:
        attributes["gen_ai.request.max_tokens"] = request["max_tokens"]
    # Parametri effettivi della richiesta, non i massimali di configurazione del ciclo.
    attributes["llm.invocation_parameters"] = json.dumps(
        {
            k: request[k]
            for k in ("max_tokens", "temperature", "top_p", "thinking", "reasoning_effort")
            if request.get(k) is not None
        },
        sort_keys=True,
    )
    for field in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "cache_n"):
        value = sample.get("server_reported_timings", {}).get(field)
        if value is not None:
            attributes["tesi.request.server." + field] = value
    return attributes


@contextmanager
def request_span(telemetry, observation, model):
    if telemetry is None:
        yield
        return
    index = observation.request_metadata["index"]
    with telemetry.span(
        f"chat.request.{index}",
        kind=SpanKind.CLIENT,
        attributes=request_attributes(model, observation.snapshot(complete=False)),
    ) as span:
        try:
            yield
        finally:
            span.set_attributes(
                request_attributes(
                    model, observation.snapshot(complete=observation.provider_complete)
                )
            )


def agent_attributes(metrics, tools):
    attributes = {
        "openinference.span.kind": "AGENT",
        "tesi.measurement.boundary": "agent_run_client",
        "tesi.agent.context.transport": "stateless_chat_completions",
        "tesi.agent.request_count": metrics.get("model_requests_started", 0),
        "tesi.agent.usage.available": metrics.get("usage_available", False),
        "tesi.agent.usage.complete": metrics.get("usage_complete", False),
    }
    for field in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "subsequent_request_input_tokens",
    ):
        if metrics.get(field) is not None:
            attributes["tesi.agent.usage." + field + "_total"] = metrics[field]
    for field in ("first_request_input_tokens", "max_request_input_tokens"):
        if metrics.get(field) is not None:
            attributes["tesi.agent.usage." + field] = metrics[field]
    for source, target in {
        "cycle_duration_seconds": "cycle_seconds",
        "provider_requests_duration_seconds": "provider_requests_seconds",
        "between_requests_seconds": "between_requests_seconds",
        "time_to_first_text_seconds": "first_any_text_seconds",
        "time_to_first_tool_call_seconds": "first_tool_call_seconds",
        "time_to_final_response_first_text_seconds": "final_response_first_text_seconds",
    }.items():
        if metrics.get(source) is not None:
            attributes["tesi.agent.timing." + target] = metrics[source]
    if metrics.get("finish_reason"):
        attributes["tesi.agent.finish_reason"] = _finish_reason(metrics["finish_reason"])
    attempts, calls = tools.get("attempts", []), tools.get("calls", [])
    attributes.update(
        {
            "tesi.agent.tools.attempts": len(attempts),
            "tesi.agent.tools.rejected": sum(a.get("args_valid") is False for a in attempts),
            "tesi.agent.tools.executed": len(calls),
            "tesi.agent.tools.completed": sum(c.get("status") == "completed" for c in calls),
            "tesi.agent.tools.failed": sum(c.get("status") == "failed" for c in calls),
        }
    )
    return attributes


def prompt_attributes(request):
    return {
        "openinference.span.kind": "AGENT",
        "tesi.prompt.initial_input_tokens_estimate": request.input_tokens_estimate,
        "tesi.prompt.estimate_method": request.input_estimate_method,
        "tesi.prompt.source_count": len(request.catalog.sources),
        "tesi.prompt.passage_count": len(request.catalog.evidences),
        "tesi.prompt.full_context": True,
        "tesi.prompt.instruction_characters": len(request.instructions),
        "tesi.prompt.data_characters": len(request.prompt),
        **{"tesi.prompt." + name: count for name, count in request.input_tokens_breakdown.items()},
    }
