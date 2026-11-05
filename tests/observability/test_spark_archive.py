import io
import json
from urllib.error import URLError

import pytest

from codice_tesi.config import ModelConfig
from codice_tesi.observability import spark_archive
from codice_tesi.observability.execution import ExecutionRecord, ExperimentConfig
from codice_tesi.observability.spark import save_snapshot


def test_archive_keeps_attempts_distinct_and_recovers_reference_after_restart(
    tmp_path, monkeypatch
):
    output = tmp_path / "output"
    runs = output / "dimissioni-mimic"
    archive = tmp_path / "TEST-SPARK"
    version = "a" * 64
    config = ModelConfig(thinking=False, tools_enabled=True)
    metrics = {"input_tokens": 42, "output_tokens": 12, "reasoning_tokens": None}
    sample = {"t": 1, "gpu_utilization": 75}

    def create_run(*, status, filename=None, text=None, dry_run=False):
        record = ExecutionRecord(
            runs,
            experiment=ExperimentConfig(),
            config=config,
            context_id="mimic-1",
            dry_run=dry_run,
        )
        directory = runs / "mimic-1" / record.path.stem
        directory.mkdir(parents=True)
        if filename:
            (directory / filename).write_text(text, encoding="utf-8")
        record.finish(status, llm=metrics)
        record.data["telemetry"] = {"enabled": False}
        if not dry_run:
            hardware_path = f"spark/{record.path.stem}.json"
            save_snapshot(runs / hardware_path, {"run": {"series": [sample]}})
            record.data["spark_monitor"] = {
                "status": "captured",
                "path": hardware_path,
                "sample_count": 1,
            }
            record.data["test_spark"] = {
                "context_id": "mimic-1",
                "dataset_version": version,
                "artifact_directory": directory.relative_to(runs).as_posix(),
            }
        record.save()
        return record.path.stem

    completed = create_run(status="completed", filename="bozza.txt", text="Bozza di prova")
    create_run(status="prepared", dry_run=True)

    class EndScan(Exception):
        pass

    def end_scan(_):
        raise EndScan

    def unavailable(*args, **kwargs):
        raise URLError("servizio temporaneamente indisponibile")

    def scan_once():
        with pytest.raises(EndScan):
            spark_archive.run_worker(archive, output, base_url="http://reference", token="private")

    monkeypatch.setattr(spark_archive, "sleep", end_scan)
    monkeypatch.setattr(spark_archive, "urlopen", unavailable)
    scan_once()
    first = archive / "TEST-CASO-MIMIC-1"
    sheet = spark_archive.read_json(first / "scheda.json")
    assert sheet["run_id"] == completed
    assert sheet["model"] == config.model
    assert sheet["reasoning_enabled"] is False and sheet["reasoning_effort"] is None
    assert sheet["tools_enabled"] is True
    assert sheet["reference"] == {"status": "pending"}
    assert (first / "bozza.txt").read_text() == "Bozza di prova"
    report = spark_archive.read_json(first / "metriche.json")
    assert report["token_e_stream"] == metrics
    assert report["hardware_spark"]["samples_in_run"] == [sample]

    def reference(request, *, timeout):
        assert request.full_url == (
            f"http://reference/api/v1/revisione/contesti/mimic-1/lettera-originale?versione={version}"
        )
        assert request.headers["Authorization"] == "Bearer private"
        return io.BytesIO(json.dumps({"note_id": "note-1", "text": "Originale"}).encode())

    monkeypatch.setattr(spark_archive, "urlopen", reference)
    failed = create_run(status="failed", filename="risposta.parziale.json", text="{")
    scan_once()
    scan_once()
    second = archive / "TEST-CASO-MIMIC-2"
    assert set(archive.iterdir()) == {first, second}
    assert (first / "lettera_ufficiale.txt").read_text() == "Originale"
    assert (second / "risposta.parziale.json").read_text() == "{"
    sheet = spark_archive.read_json(second / "scheda.json")
    assert sheet["run_id"] == failed and sheet["status"] == "failed"
    assert sheet["reference"]["note_id"] == "note-1"
