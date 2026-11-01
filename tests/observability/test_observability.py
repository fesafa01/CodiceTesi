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
            yield chunk(delta={"content": letter}, finish="stop")
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


@pytest.mark.parametrize(
    "probe_available,model_fails", [(True, False), (False, False), (True, True)]
)
def test_hardware_is_correlated_and_failure_does_not_change_generation(
    tmp_path, monkeypatch, packet_factory, letter_factory, probe_available, model_fails
):
    from datetime import UTC, datetime

    from codice_tesi.observability import hardware
    from codice_tesi.observability.hardware import HardwareConfig

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(observability, "OTLPSpanExporter", lambda **kwargs: exporter)
    monkeypatch.setattr(observability, "OTLPMetricExporter", lambda **kwargs: MemoryMetrics())

    async def fetch(*args, **kwargs):
        return packet_factory("diario")

    calls = []

    def read_hardware(config):
        calls.append(True)
        if not probe_available:
            raise OSError("PRIVATE_HARDWARE_ERROR")
        return {
            "schema_version": "1.0",
            "boot_id": "a" * 32,
            "sequence": len(calls),
            "sampled_at": datetime.now(UTC).isoformat(),
            "host": "fixture-spark",
            "sources": {source: {"available": True} for source in ("cpu", "memory", "disk", "gpu")},
            "metrics": [
                {"name": "spark_gpu_temperature_celsius", "labels": {"gpu": "0"}, "value": 55}
            ],
        }

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk(delta={"role": "assistant"})
            async with asyncio.timeout(2):
                while not calls:
                    await asyncio.sleep(0.005)
            # Consente al task di convalidare e scrivere il campione.
            await asyncio.sleep(0.02)
            if model_fails:
                raise RuntimeError("PRIVATE_MODEL_ERROR")
            yield chunk(delta={"content": letter_factory()}, finish="stop")
            yield b"data: [DONE]\n\n"

    monkeypatch.setattr(hardware, "fetch_snapshot", read_hardware)
    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setattr(api, "create_client", lambda *a, **kw: client_for(Stream()))
    invocation = application.run_application(
        backend_url="http://test",
        output_dir=tmp_path,
        telemetry_config=TelemetryConfig(enabled=True, _env_file=None),
        hardware_config=HardwareConfig(enabled=True, _env_file=None),
    )
    if model_fails:
        with pytest.raises(RuntimeError):
            asyncio.run(invocation)
    else:
        asyncio.run(invocation)
    record = json.loads(next((tmp_path / "registro").glob("*.json")).read_text())
    assert record["status"] == ("failed" if model_fails else "completed")
    measured = record["hardware"]
    assert measured["status"] == ("available" if probe_available else "unavailable")
    assert measured["samples"] == (1 if probe_available else 0)
    assert "cost_estimate" not in measured
    spans = exporter.get_finished_spans()
    for span in (s for s in spans if s.name == "discharge.execution" or s.kind.name == "CLIENT"):
        assert span.attributes["tesi.hardware.status"] == measured["status"]
    if probe_available:
        assert measured["statistics"][0]["mean"] == 55
        assert record["run_id"] in measured["raw_samples_path"]
    dumped = json.dumps(record) + "\n".join(s.to_json() for s in spans)
    assert "PRIVATE_HARDWARE_ERROR" not in dumped
    assert "PRIVATE_MODEL_ERROR" not in dumped
