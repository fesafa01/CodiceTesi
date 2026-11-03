"""Span espliciti dei tool: solo nomi autorizzati, contatori e codici sicuri."""

from contextlib import contextmanager

from opentelemetry.trace import StatusCode


@contextmanager
def tool_span(telemetry, name, index, *, request_index=None):
    if telemetry is None:
        yield None
        return
    with telemetry.span(
        "tool " + name,
        attributes={
            "openinference.span.kind": "TOOL",
            "tool.name": name,
            "gen_ai.tool.name": name,
            "tesi.tool.call_index": index,
            "tesi.tool.status": "started",
        },
    ) as span:
        if request_index is not None:
            span.set_attribute("tesi.tool.request_index", request_index)
        try:
            yield span
        except BaseException:
            span.set_attribute("tesi.tool.status", "failed")
            raise
        else:
            span.set_attribute("tesi.tool.status", "completed")


def trace_rejected_tool(telemetry, safe_name, index, *, request_index=None):
    if telemetry is None:
        return
    with telemetry.span(
        "tool " + safe_name,
        attributes={
            "openinference.span.kind": "TOOL",
            "tool.name": safe_name,
            "gen_ai.tool.name": safe_name,
            "tesi.tool.status": "rejected",
            "tesi.tool.arguments_valid": False,
            "tesi.tool.attempt_index": index,
            "error.type": "ToolArgumentsRejected",
        },
    ) as span:
        if request_index is not None:
            span.set_attribute("tesi.tool.request_index", request_index)
        span.set_status(StatusCode.ERROR)
