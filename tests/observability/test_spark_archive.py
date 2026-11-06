import io
import json
import os
from shutil import rmtree
from urllib.error import HTTPError, URLError

import pytest

from codice_tesi.config import ModelConfig
from codice_tesi.observability import spark_archive
from codice_tesi.observability.execution import ExecutionRecord, ExperimentConfig
from codice_tesi.observability.spark import save_snapshot


def test_archive_publishes_only_complete_current_runs_after_reference_is_available(
    tmp_path, monkeypatch
):
    output = tmp_path / "output"
    runs = output / "dimissioni-mimic"
    archive = tmp_path / "TEST-SPARK"
    version = "a" * 64
    config = ModelConfig(thinking=False, tools_enabled=True)
    metrics = {"input_tokens": 42, "output_tokens": 12, "reasoning_tokens": None}
    sample = {"t": 1, "gpu_utilization": 75}

    def create_run(*, status, filename=None, text=None, dry_run=False, legacy=False, evidence=True):
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
        if filename == "bozza.txt" and evidence:
            (directory / "evidenze.md").write_text("Fonte sintetica", encoding="utf-8")
        record.finish(status, llm=metrics)
        record.data["prompt_version"] = "fixture-prompt"
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
            if not legacy:
                record.data["test_spark"]["archive_version"] = spark_archive.ARCHIVE_VERSION
        record.save()
        return record.path.stem

    completed = create_run(status="completed", filename="bozza.txt", text="Bozza di prova")
    for _ in range(4):
        create_run(status="completed", filename="bozza.txt", text="Storica", legacy=True)
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
    assert list(archive.iterdir()) == []

    def reference(request, *, timeout):
        assert request.full_url == (
            f"http://reference/api/v1/revisione/contesti/mimic-1/lettera-originale?versione={version}"
        )
        assert request.headers["Authorization"] == "Bearer private"
        return io.BytesIO(json.dumps({"note_id": "note-1", "text": "Originale"}).encode())

    monkeypatch.setattr(spark_archive, "urlopen", reference)
    scan_once()
    assert set(archive.iterdir()) == {first}
    assert {file.name for file in first.iterdir()} == {
        "bozza.txt",
        "evidenze.md",
        "lettera_ufficiale.txt",
        "metriche.json",
        "scheda.json",
    }
    sheet = spark_archive.read_json(first / "scheda.json")
    assert sheet["run_id"] == completed
    assert sheet["model"] == config.model
    assert sheet["reasoning_enabled"] is False and sheet["reasoning_effort"] is None
    assert sheet["tools_enabled"] is True
    assert sheet["reference"]["status"] == "saved"
    assert (first / "bozza.txt").read_text() == "Bozza di prova"
    report = spark_archive.read_json(first / "metriche.json")
    assert report["prompt_version"] == "fixture-prompt"
    assert report["token_e_stream"] == metrics
    assert report["hardware_spark"]["samples_in_run"] == [sample]

    for status in ("failed", "interrupted", "running"):
        create_run(status=status, filename="bozza.txt", text="Artefatto di prova")
    create_run(status="completed", filename="risposta.parziale.json", text="{")
    create_run(status="completed", filename="bozza.txt", text="")
    create_run(status="completed", filename="bozza.txt", text="Senza fonti", evidence=False)
    scan_once()
    scan_once()
    assert set(archive.iterdir()) == {first}
    assert (first / "lettera_ufficiale.txt").read_text() == "Originale"

    another = create_run(status="completed", filename="bozza.txt", text="Seconda bozza")
    scan_once()
    second = archive / "TEST-CASO-MIMIC-2"
    assert set(archive.iterdir()) == {first, second}
    assert (second / "bozza.txt").read_text() == "Seconda bozza"
    sheet = spark_archive.read_json(second / "scheda.json")
    assert sheet["run_id"] == another and sheet["status"] == "completed"
    assert sheet["reference"]["note_id"] == "note-1"

    def missing_reference(request, *, timeout):
        raise HTTPError(request.full_url, 404, "Originale assente", None, None)

    create_run(status="completed", filename="bozza.txt", text="Senza originale")
    monkeypatch.setattr(spark_archive, "urlopen", missing_reference)
    scan_once()
    assert set(archive.iterdir()) == {first, second}

    legacy = archive / "TEST-CASO-MIMIC-3"
    legacy.mkdir()
    save_snapshot(
        legacy / "scheda.json",
        {
            **sheet,
            "run_id": "legacy-failed",
            "status": "failed",
            "draft_file": None,
            "reference": {"status": "pending"},
        },
    )
    scan_once()
    assert spark_archive.read_json(legacy / "scheda.json")["reference"] == {"status": "pending"}
    monkeypatch.setattr(spark_archive, "urlopen", reference)

    # Un ripristino dell'archivio riparte da 1 e ignora i record precedenti.
    rmtree(first)
    rmtree(second)
    rmtree(legacy)
    marker = archive / spark_archive.RESET_MARKER
    marker.touch()
    reset_at = marker.stat().st_mtime_ns
    for path in (runs / "registro").glob("*.json"):
        os.utime(path, ns=(reset_at, reset_at - 1_000_000_000))
    new_run = create_run(status="completed", filename="bozza.txt", text="Nuova prova")
    new_path = runs / "registro" / f"{new_run}.json"
    os.utime(new_path, ns=(reset_at, reset_at + 1_000_000_000))

    scan_once()
    scan_once()
    assert {path for path in archive.iterdir() if path.is_dir()} == {first}
    assert spark_archive.read_json(first / "scheda.json")["run_id"] == new_run
