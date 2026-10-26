"""Registro e artefatti utilizzabili anche senza un backend di osservabilità."""

import json

from codice_tesi.config import ModelConfig
from codice_tesi.discharge.artifacts import RunArtifacts
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.observability.execution import ExecutionRecord, ExperimentConfig, export_registry


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
