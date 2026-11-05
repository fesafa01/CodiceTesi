"""Contratto delle misure, isolamento dei dati e registro anche nei fallimenti."""

import asyncio
import csv
import json

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from codice_tesi import api, application
from codice_tesi.config import ModelConfig
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.generator import LLMDraftGenerator
from codice_tesi.observability import telemetry as observability
from codice_tesi.observability.execution import ExperimentConfig, export_registry
from codice_tesi.observability.telemetry import TelemetryConfig


@pytest.fixture
def letter_factory():
    """Bozza di protocollo indipendente dalle fixture della validazione clinica."""
    from codice_tesi.discharge.template import load_template

    def create():
        return "BOZZA DI LETTERA DI DIMISSIONE - DA REVISIONARE\n" + "\n".join(
            f"{index}. {section.title}\n"
            + (
                "Non disponibile nelle note fornite."
                if section.id == "istologico_in_attesa"
                else ""
                if section.id == "informazioni_istologico"
                else "Testo di fixture."
            )
            + "\n"
            for index, section in enumerate(load_template().sections, 1)
        )

    return create


def chunk(*, delta=None, usage=None, finish=None):
    body = {
        "id": "test-observation",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "served-model",
        "choices": [],
    }
    if delta is not None or finish is not None:
        body["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    if usage is not None:
        body["usage"] = usage
    return b"data: " + json.dumps(body).encode() + b"\n\n"


def client_for(stream):
    def handler(request):
        body = json.loads(request.content)
        assert body["stream_options"]["include_usage"] is True
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    return AsyncOpenAI(
        base_url="http://test/v1",
        api_key="never-export-this-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize("reported", [None, (0, 0, 0), (100, 25, 15)])
def test_missing_usage_is_not_zero_and_cumulative_usage_is_not_added(packet_factory, reported):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk(delta={"role": "assistant"})
            await asyncio.sleep(0.01)
            yield chunk(delta={"reasoning_content": "PRIVATE_REASONING"})
            await asyncio.sleep(0.01)
            yield chunk(delta={"content": "Lettera"}, finish="stop")
            if reported is not None:
                inp, out, reasoning = reported
                usage = {
                    "prompt_tokens": inp,
                    "completion_tokens": out,
                    "total_tokens": inp + out,
                    "completion_tokens_details": {"reasoning_tokens": reasoning},
                }
                yield chunk(usage=usage)
                yield chunk(usage=usage)
            yield b"data: [DONE]\n\n"

    async def run():
        async with client_for(Stream()) as client:
            generator = LLMDraftGenerator(ModelConfig(), client)
            request = prepare_request(packet_factory("diario"), ContextConfig(), output_tokens=100)
            assert "".join([part async for part in generator.stream(request)]) == "Lettera"
            return generator.usage["metrics"]

    metrics = asyncio.run(run())
    assert metrics["time_to_first_chunk_seconds"] < metrics["time_to_first_reasoning_seconds"]
    assert metrics["time_to_first_reasoning_seconds"] < metrics["time_to_first_text_seconds"]
    assert metrics["server_ttft_seconds"] is None
    assert metrics["server_decode_tokens_per_second"] is None
    assert metrics["usage_available"] is (reported is not None)
    if reported is None:
        assert metrics["input_tokens"] is None and metrics["reasoning_tokens"] is None
        assert metrics["output_tokens_per_request_second"] is None
    else:
        assert metrics["input_tokens"] == reported[0]
        assert metrics["output_tokens"] == reported[1]
        assert metrics["reasoning_tokens"] == reported[2]
        assert metrics["visible_output_tokens"] == reported[1] - reported[2]
    assert "PRIVATE_REASONING" not in json.dumps(metrics)


def test_backend_failure_has_atomic_exportable_record(tmp_path, monkeypatch):
    async def fetch(*args, **kwargs):
        raise ValueError("PRIVATE_CLINICAL_ERROR")

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    with pytest.raises(ValueError):
        asyncio.run(
            application.run_application(
                backend_url="http://test",
                output_dir=tmp_path,
                telemetry_config=TelemetryConfig(enabled=False),
            )
        )
    path = next((tmp_path / "registro").glob("*.json"))
    record = json.loads(path.read_text())
    assert record["status"] == "failed" and record["failure_stage"] == "fetch_packet"
    assert record["llm"] is None and record["artifact_directory"] is None
    assert "PRIVATE_CLINICAL_ERROR" not in path.read_text()
    assert record["timings"]["total_seconds"] >= record["timings"]["fetch_packet_seconds"]
    for format in ("jsonl", "csv"):
        destination = tmp_path / ("export." + format)
        assert export_registry(tmp_path, destination, format=format) == 1
        if format == "jsonl":
            assert json.loads(destination.read_text())["llm"] is None
        else:
            with destination.open() as stream:
                row = next(csv.DictReader(stream))
            assert row["run_id"] == record["run_id"]
    with pytest.raises(ValueError, match="sovrascrivere"):
        export_registry(tmp_path, path, format="jsonl")


class MemoryMetrics(MetricExporter):
    def __init__(self, **kwargs):
        super().__init__(preferred_temporality=kwargs.get("preferred_temporality"))
        self.exports = []

    def export(self, metrics_data, timeout_millis=10000, **kwargs):
        self.exports.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10000):
        return True

    def shutdown(self, timeout_millis=30000, **kwargs):
        pass


def test_trace_and_registry_correlate_without_content_or_duplicate_llm_spans(
    tmp_path,
    monkeypatch,
    packet_factory,
    letter_factory,
    response_factory,
):
    exporter = InMemorySpanExporter()
    metrics_exporter = MemoryMetrics()
    monkeypatch.setattr(observability, "OTLPSpanExporter", lambda **kwargs: exporter)
    monkeypatch.setattr(observability, "OTLPMetricExporter", lambda **kwargs: metrics_exporter)
    packet = packet_factory("diario", text="PRIVATE_SOURCE_TEXT")
    letter = letter_factory()

    async def fetch(*args, **kwargs):
        return packet

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk(delta={"reasoning_content": "PRIVATE_REASONING"})
            yield chunk(delta={"content": response_factory(letter)}, finish="stop")
            yield chunk(
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "total_tokens": 140,
                    "completion_tokens_details": {"reasoning_tokens": 10},
                }
            )
            yield b"data: [DONE]\n\n"

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(api, "create_client", lambda *a, **kw: client_for(Stream()))
    directory = asyncio.run(
        application.run_application(
            backend_url="http://test",
            output_dir=tmp_path,
            experiment=ExperimentConfig(experiment_id="measurement-test", repetition=1),
            telemetry_config=TelemetryConfig(enabled=True, _env_file=None),
        )
    )
    record = json.loads(next((tmp_path / "registro").glob("*.json")).read_text())
    spans = exporter.get_finished_spans()
    assert len([s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"]) == 1
    assert all(f"{s.context.trace_id:032x}" == record["trace_id"] for s in spans)
    root = next(s for s in spans if s.name == "discharge.execution")
    assert root.attributes["tesi.run_id"] == record["run_id"]
    llm = next(s for s in spans if s.attributes.get("openinference.span.kind") == "LLM")
    agent = next(s for s in spans if s.name == "discharge.agent_run")
    assert agent.attributes["tesi.prompt.instruction_tokens"] > 0
    assert agent.attributes["tesi.prompt.source_text_tokens_sum"] > 0
    assert agent.attributes["tesi.agent.request_count"] == 1
    assert llm.parent.span_id == agent.context.span_id
    assert llm.attributes["llm.token_count.completion"] == 40
    assert llm.attributes["gen_ai.usage.reasoning.output_tokens"] == 10
    assert "llm.token_count.completion" not in root.attributes
    dumped = "\n".join(s.to_json() for s in spans)
    for secret in ("PRIVATE_SOURCE_TEXT", "PRIVATE_REASONING", letter, "never-export-this-key"):
        assert secret not in dumped
    assert record["status"] == "completed" and record["llm"]["visible_output_tokens"] == 30
    assert record["experiment"]["model_revision"] is None
    assert record["request_sha256"] and record["software"]["source_sha256"]
    assert record["timings"]["validate_and_save_seconds"] >= 0
    assert json.loads((directory / "esito.json").read_text())["run_id"] == record["run_id"]
    assert metrics_exporter.exports
    names = {
        m.name
        for batch in metrics_exporter.exports
        for resource in batch.resource_metrics
        for scope in resource.scope_metrics
        for m in scope.metrics
    }
    assert "tesi.executions" in names and "gen_ai.client.token.usage" in names


def test_inconsistent_token_contract_does_not_create_fake_visible_tokens(packet_factory):
    from openai.types.chat import ChatCompletionChunk

    from codice_tesi.observability.stream_metrics import StreamObservation

    observation = StreamObservation()
    observation.start()
    observation.observe(
        ChatCompletionChunk.model_validate(
            {
                "id": "test",
                "created": 1,
                "model": "test",
                "object": "chat.completion.chunk",
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "completion_tokens_details": {"reasoning_tokens": 8},
                },
            }
        )
    )
    metrics = observation.snapshot(complete=True)
    assert metrics["visible_output_tokens"] is None
    assert metrics["usage_issues"] == ["reasoning_exceeds_output"]


def test_experiment_metadata_rejects_unknown_fields():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate({"concurreny": 4})


def test_source_manifest_excludes_files_not_in_installed_distribution(tmp_path, monkeypatch):
    from pathlib import Path, PurePosixPath

    from codice_tesi.observability import execution

    root = tmp_path / "codice_tesi"
    root.mkdir()
    current = root / "application.py"
    current.write_text("# installed code")
    (root / "application 2.py").write_text("# residual copy, not installed")

    class Package:
        files = [PurePosixPath("codice_tesi/application.py")]

        def locate_file(self, file):
            return tmp_path / file

    monkeypatch.setattr(execution, "distribution", lambda name: Package())
    assert execution._source_files(root) == [Path(current)]


def test_export_or_shutdown_failure_does_not_change_local_result(
    tmp_path,
    monkeypatch,
    packet_factory,
):
    class FailedSpans(InMemorySpanExporter):
        def export(self, spans):
            return SpanExportResult.FAILURE

        def shutdown(self):
            raise OSError("exporter unavailable")

    class FailedMetrics(MemoryMetrics):
        def export(self, *args, **kwargs):
            return MetricExportResult.FAILURE

        def shutdown(self, **kwargs):
            raise TimeoutError("exporter unavailable")

    monkeypatch.setattr(observability, "OTLPSpanExporter", lambda **kwargs: FailedSpans())
    monkeypatch.setattr(observability, "OTLPMetricExporter", lambda **kwargs: FailedMetrics())

    async def fetch(*args, **kwargs):
        return packet_factory("diario")

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    directory = asyncio.run(
        application.run_application(
            backend_url="http://test",
            output_dir=tmp_path,
            dry_run=True,
            telemetry_config=TelemetryConfig(enabled=True, _env_file=None),
        )
    )
    record = json.loads(next((tmp_path / "registro").glob("*.json")).read_text())
    assert directory.is_dir() and record["status"] == "prepared"
    assert record["telemetry"]["shutdown_error_type"]
    assert "exporter unavailable" not in json.dumps(record)
    assert record["telemetry"]["export_confirmed"] is None


def test_prometheus_records_requests_once_and_cycle_separately(monkeypatch):
    from types import SimpleNamespace

    spans = InMemorySpanExporter()
    metrics = MemoryMetrics()
    monkeypatch.setattr(observability, "OTLPSpanExporter", lambda **kwargs: spans)
    monkeypatch.setattr(observability, "OTLPMetricExporter", lambda **kwargs: metrics)
    telemetry = observability.Telemetry(TelemetryConfig(enabled=True, _env_file=None))
    record = SimpleNamespace(
        data={
            "status": "completed",
            "dry_run": False,
            "model_config": ModelConfig().model_dump(),
            "timings": {"total_seconds": 12},
            "llm": {
                "cycle_duration_seconds": 10,
                "model_requests_started": 2,
                "input_tokens": 270,
                "output_tokens": 50,
                "requests": [
                    {
                        "request_duration_seconds": 4,
                        "usage_complete": True,
                        "usage_issues": [],
                        "input_tokens": 100,
                        "output_tokens": 20,
                    },
                    {
                        "request_duration_seconds": 5,
                        "usage_complete": True,
                        "usage_issues": [],
                        "input_tokens": 170,
                        "output_tokens": 30,
                    },
                ],
            },
        }
    )
    telemetry.record_metrics(record)
    telemetry.close()
    instruments = {
        metric.name: metric
        for batch in metrics.exports
        for resource in batch.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    tokens = instruments["gen_ai.client.token.usage"].data.data_points
    assert sum(p.count for p in tokens) == 4
    assert sum(p.sum for p in tokens if p.attributes["gen_ai.token.type"] == "input") == 270
    assert sum(p.sum for p in tokens if p.attributes["gen_ai.token.type"] == "output") == 50
    duration = instruments["gen_ai.client.operation.duration"].data.data_points
    assert sum(p.count for p in duration) == 2 and sum(p.sum for p in duration) == 9
    cycle = instruments["tesi.agent.duration"].data.data_points
    assert sum(p.count for p in cycle) == 1 and sum(p.sum for p in cycle) == 10
