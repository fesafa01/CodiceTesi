"""Una richiesta = uno span LLM; totali del ciclo separati e nessun contenuto."""

import asyncio
import json
from time import monotonic

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from codice_tesi import api, application
from codice_tesi.config import ModelConfig
from codice_tesi.observability.model_spans import agent_attributes, request_attributes
from codice_tesi.observability.stream_metrics import RunStreamObservation
from codice_tesi.observability.telemetry import Telemetry, TelemetryConfig


def usage_chunk(inp, out, cached=None):
    usage = {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return ChatCompletionChunk.model_validate(
        {
            "id": "fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [],
            "usage": usage,
        }
    )


@pytest.mark.parametrize(
    "cached,status,uncached",
    [
        (None, "unreported", None),
        (0, "reported_zero", 100),
        (80, "reported_reuse", 20),
        (101, "inconsistent", None),
    ],
)
def test_cache_status_does_not_equate_missing_usage_with_zero(cached, status, uncached):
    run = RunStreamObservation()
    sample = run.begin({"max_tokens": 25000, "history_mode": "final_append"})
    sample.observe(usage_chunk(100, 10, cached))
    sample.ended = monotonic()
    sample.provider_complete = True
    metrics = sample.snapshot(complete=True)
    attributes = request_attributes("fixture-model", metrics)
    assert attributes["tesi.request.cache.status"] == status
    assert metrics["uncached_input_tokens"] == uncached
    assert attributes["gen_ai.request.max_tokens"] == 25000
    assert attributes["tesi.request.context.history_mode"] == "final_append"
    if uncached is None:
        assert "tesi.request.usage.uncached_input_tokens" not in attributes
    if status == "inconsistent":
        assert metrics["usage_issues"] == ["cached_exceeds_input"]
        assert metrics["output_tokens_per_request_second"] is None


def test_run_totals_are_separate_from_largest_prompt_and_do_not_sum_usage_chunks():
    run = RunStreamObservation()
    first = run.begin()
    first.started = 10
    first.ended = 20
    first.provider_complete = True
    first.observe(usage_chunk(42262, 333, 0))
    first.observe(usage_chunk(42262, 333, 0))
    first.finish_reason = "tool_calls"
    first.first_text = 18  # Un eventuale preambolo non è la risposta finale.
    second = run.begin()
    second.started = 21
    second.ended = 50
    second.provider_complete = True
    second.observe(usage_chunk(44097, 3884, 0))
    second.finish_reason = "stop"
    second.first_text = 25
    metrics = run.snapshot(complete=True)
    assert metrics["input_tokens"] == 86359
    assert metrics["total_tokens"] == 90576
    assert metrics["max_request_input_tokens"] == 44097
    assert metrics["first_request_input_tokens"] == 42262
    assert metrics["subsequent_request_input_tokens"] == 44097
    assert metrics["request_duration_seconds"] == 40
    assert metrics["provider_requests_duration_seconds"] == 39
    assert metrics["between_requests_seconds"] == 1
    assert metrics["time_to_first_text_seconds"] == 8
    assert metrics["time_to_final_response_first_text_seconds"] == 15
    attributes = agent_attributes(metrics, {})
    assert attributes["tesi.agent.request_count"] == 2
    assert attributes["tesi.agent.usage.input_tokens_total"] == 86359
    assert attributes["tesi.agent.usage.max_request_input_tokens"] == 44097
    assert not any(k.startswith(("llm.token_count", "gen_ai.usage")) for k in attributes)


def test_missing_usage_in_one_request_keeps_aggregate_unknown():
    run = RunStreamObservation()
    first = run.begin()
    first.provider_complete = True
    first.ended = monotonic()
    first.observe(usage_chunk(100, 20))
    second = run.begin()
    second.ended = monotonic()
    metrics = run.snapshot(complete=False)
    assert metrics["input_tokens"] is None
    assert metrics["max_request_input_tokens"] is None
    assert not metrics["usage_complete"]
    assert len(metrics["requests"]) == 2
    attributes = agent_attributes(metrics, {})
    assert "tesi.agent.usage.input_tokens_total" not in attributes


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_cancelled_request_span_is_closed_without_content(
    cancel, packet_factory, monkeypatch, tmp_path
):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry(TelemetryConfig(enabled=False, _env_file=None))
    telemetry.tracer = provider.get_tracer("request-failure-test")
    monkeypatch.setattr(application, "Telemetry", lambda _: telemetry)

    async def check():
        entered = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                if not cancel:
                    raise RuntimeError("PRIVATE_TRANSPORT_FAILURE")
                await asyncio.Event().wait()
                yield b""  # Async iterator, mai raggiunto.

        def client(*args, **kwargs):
            return AsyncOpenAI(
                base_url="http://fixture/v1",
                api_key="PRIVATE_API_KEY",
                max_retries=0,
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(
                            200, headers={"Content-Type": "text/event-stream"}, stream=Stream()
                        )
                    )
                ),
            )

        monkeypatch.setattr(api, "create_client", client)
        task = asyncio.create_task(
            application.run_application(
                backend_url="http://fixture",
                packet=packet_factory("nota", text="PRIVATE_SOURCE"),
                output_dir=tmp_path,
                config=ModelConfig(thinking=False),
            )
        )
        if cancel:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task

    try:
        asyncio.run(check())
        spans = exporter.get_finished_spans()
        requests = [s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"]
        assert len(requests) == 1
        span = requests[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.end_time >= span.start_time
        assert span.attributes["tesi.request.stream_complete"] is False
        assert span.attributes["tesi.usage.available"] is False
        assert "llm.token_count.prompt" not in span.attributes
        assert not span.events
        serialized = "\n".join(s.to_json() for s in spans)
        for private in ("PRIVATE_TRANSPORT_FAILURE", "PRIVATE_SOURCE", "PRIVATE_API_KEY"):
            assert private not in serialized
        record = json.loads(next((tmp_path / "registro").glob("*.json")).read_text())
        assert record["status"] == ("interrupted" if cancel else "failed")
        assert record["llm"]["model_requests_started"] == 1
    finally:
        provider.shutdown()
