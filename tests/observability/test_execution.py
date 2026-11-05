"""Registro e artefatti utilizzabili anche senza un backend di osservabilità."""

import asyncio
import json
from datetime import datetime
from io import StringIO
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest

from codice_tesi import application
from codice_tesi.config import SPARK_LLM_URL, ModelConfig, Settings
from codice_tesi.discharge.artifacts import RunArtifacts
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.models import MIMIC_DATASET
from codice_tesi.observability import spark
from codice_tesi.observability.execution import ExecutionRecord, ExperimentConfig, export_registry
from codice_tesi.observability.telemetry import TelemetryConfig


def test_registry_correlates_artifacts_and_exports_without_clinical_text(
    tmp_path,
    packet_factory,
):
    packet = packet_factory("diario", text="PRIVATE_SOURCE_TEXT")
    config = ModelConfig()
    context_config = ContextConfig()
    request = prepare_request(packet, context_config, output_tokens=config.max_tokens)
    record = ExecutionRecord(
        tmp_path,
        experiment=ExperimentConfig(repetition=1),
        config=config,
        context_id=packet.context.id,
        dry_run=True,
    )
    artifacts = RunArtifacts(
        tmp_path,
        packet,
        request,
        seed=42,
        fixed_context=True,
        config=config,
        preview=True,
    )
    record.bind(packet, request, artifacts, context_config)
    record.finish("prepared")
    stored = json.loads(record.path.read_text())
    assert stored["status"] == "prepared" and stored["llm"] is None
    assert stored["request_sha256"] and stored["packet_sha256"]
    assert stored["input_estimate_method"] == request.input_estimate_method
    assert stored["input_tokenizer_revision"] == request.input_tokenizer_revision
    assert artifacts.report["input_estimate_method"] == request.input_estimate_method
    assert artifacts.report["input_tokenizer_revision"] == request.input_tokenizer_revision
    assert stored["experiment"]["model_revision"] is None
    assert "PRIVATE_SOURCE_TEXT" not in record.path.read_text()
    assert (
        json.loads((artifacts.directory / "esito.json").read_text())["run_id"] == (stored["run_id"])
    )
    destination = tmp_path / "runs.jsonl"
    assert export_registry(tmp_path, destination, format="jsonl") == 1
    assert json.loads(destination.read_text()) == stored


def test_multiple_attempts_have_distinct_records_and_keep_failures(tmp_path):
    records = [
        ExecutionRecord(
            tmp_path,
            experiment=ExperimentConfig(),
            config=ModelConfig(),
            context_id=None,
            dry_run=False,
        )
        for _ in range(2)
    ]
    assert records[0].path != records[1].path
    records[0].finish("failed", error=ValueError("PRIVATE_ERROR_TEXT"))
    stored = json.loads(records[0].path.read_text())
    assert stored["error_type"] == "ValueError" and stored["llm"] is None
    assert "PRIVATE_ERROR_TEXT" not in records[0].path.read_text()
    assert json.loads(records[1].path.read_text())["status"] == "preparing"


@pytest.mark.parametrize(
    ("failed_endpoints", "expected_status", "sample_count"),
    [
        (set(), "captured", 1),
        ({"energy"}, "partial", 1),
        ({"history", "energy"}, "unavailable", None),
    ],
)
def test_spark_run_saves_hardware_metrics_after_generation(
    tmp_path,
    monkeypatch,
    packet_factory,
    response_factory,
    letter_factory,
    failed_endpoints,
    expected_status,
    sample_count,
):
    draft = response_factory(letter_factory())

    class Generator:
        usage = {}

        def __init__(self, *args):
            pass

        async def stream(self, request):
            yield draft

    def monitor(url, *, timeout):
        record = json.loads(next((tmp_path / "registro").glob("*.json")).read_text())
        assert record["status"] == "completed"
        assert "test_spark" not in record
        start, end = (
            datetime.fromisoformat(record[key]).timestamp() for key in ("started_at", "ended_at")
        )
        parsed = urlsplit(url)
        assert parsed.hostname == "spark-413e.tail9c981a.ts.net"
        assert parsed.query == "hours=0.25"
        endpoint = parsed.path.removeprefix("/")
        if endpoint in failed_endpoints:
            raise URLError("PRIVATE_ERROR_TEXT")
        payload = {"wh_total": 1.2}
        if endpoint == "history":
            payload = {"series": [{"t": t} for t in (start - 1, (start + end) / 2, end + 1)]}
        return StringIO(json.dumps(payload))

    monkeypatch.setattr(application, "LLMDraftGenerator", Generator)
    monkeypatch.setattr(spark, "urlopen", monitor)
    packet = packet_factory("diario")
    packet = packet.model_copy(
        update={
            "context": packet.context.model_copy(
                update={
                    "dataset": MIMIC_DATASET,
                }
            )
        }
    )
    artifacts = asyncio.run(
        application.run_application(
            backend_url="http://test",
            output_dir=tmp_path,
            packet=packet,
            console=False,
            settings=Settings(base_url=SPARK_LLM_URL, _env_file=None),
            telemetry_config=TelemetryConfig(enabled=False),
        )
    )
    record = json.loads(next((tmp_path / "registro").glob("*.json")).read_text())
    assert record["status"] == "completed"
    assert record["test_spark"] == {
        "context_id": packet.context.id,
        "dataset_version": packet.context.dataset_version,
        "artifact_directory": artifacts.relative_to(tmp_path).as_posix(),
    }
    assert record["spark_monitor"] == {
        "status": expected_status,
        "path": f"spark/{record['run_id']}.json",
        "sample_count": sample_count,
    }
    saved = (tmp_path / record["spark_monitor"]["path"]).read_text()
    snapshot = json.loads(saved)
    assert snapshot["run"]["run_id"] == record["run_id"]
    assert len(snapshot["run"]["series"]) == (sample_count or 0)
    assert set(snapshot["errors"]) == failed_endpoints
    assert "PRIVATE_ERROR_TEXT" not in saved and "episode-x" not in saved


def test_manifest_tracks_application_and_templates_outside_observability(tmp_path, monkeypatch):
    from codice_tesi.observability import execution

    package = tmp_path / "codice_tesi"
    instrumentation = package / "observability"
    instrumentation.mkdir(parents=True)
    module = instrumentation / "execution.py"
    module.write_text("# registry")
    application = package / "application.py"
    application.write_text("# application v1")
    template = package / "discharge/templates/campus.json"
    template.parent.mkdir(parents=True)
    template.write_text('{"version": "1"}')
    monkeypatch.setattr(execution, "__file__", str(module))

    first = execution.software_manifest()["source_sha256"]
    application.write_text("# application v2")
    second = execution.software_manifest()["source_sha256"]
    template.write_text('{"version": "2"}')
    third = execution.software_manifest()["source_sha256"]

    assert first != second != third
