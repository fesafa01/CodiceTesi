import io
import json
import os
from hashlib import sha256
from shutil import rmtree
from urllib.error import HTTPError, URLError

import pytest

from codice_tesi.config import GEMMA_MODEL, ModelConfig
from codice_tesi.observability import spark_archive
from codice_tesi.observability.execution import ExecutionRecord, ExperimentConfig
from codice_tesi.observability.spark import save_snapshot


def test_archive_publishes_current_attempts_independently_of_draft_and_reference(
    tmp_path, monkeypatch
):
    output = tmp_path / "output"
    runs = output / "dimissioni-mimic"
    archive = tmp_path / "TEST-SPARK"
    version = "a" * 64
    config = ModelConfig(thinking=False, tools_enabled=True)
    metrics = {"input_tokens": 42, "output_tokens": 12, "reasoning_tokens": None}
    sample = {"t": 1, "gpu_utilization": 75}
    tool_output = [{"tool": "leggi_evidenze", "result": {"items": [{"text": "Fonte sintetica"}]}}]

    def create_run(
        *,
        status,
        filename=None,
        text=None,
        dry_run=False,
        legacy=False,
        evidence=True,
        tools=False,
        display_name=None,
        run_config=config,
        name_in_report=False,
        with_artifacts=True,
    ):
        record = ExecutionRecord(
            runs,
            experiment=ExperimentConfig(),
            config=run_config,
            context_id="mimic-1",
            dry_run=dry_run,
        )
        directory = runs / "mimic-1" / record.path.stem
        directory.mkdir(parents=True)
        if filename:
            (directory / filename).write_text(text, encoding="utf-8")
        if filename == "bozza.txt" and evidence:
            (directory / "evidenze.md").write_text("Fonte sintetica", encoding="utf-8")
        if tools:
            tool_results = directory / "risultati_tool.json"
            save_snapshot(tool_results, {"run_id": record.path.stem, "results": tool_output})
            record.data["artifact_hashes"] = {
                tool_results.name: sha256(tool_results.read_bytes()).hexdigest()
            }
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
                "artifact_directory": directory.relative_to(runs).as_posix()
                if with_artifacts
                else None,
            }
            if not legacy:
                record.data["test_spark"]["archive_version"] = spark_archive.ARCHIVE_VERSION
            if name_in_report:
                save_snapshot(
                    directory / "esito.json", {"contesto": {"display_name": display_name}}
                )
            elif display_name is not None:
                record.data["test_spark"]["display_name"] = display_name
        record.save()
        return record.path.stem

    completed = create_run(
        status="completed", filename="bozza.txt", text="Bozza di prova", tools=True
    )
    for _ in range(4):
        create_run(status="completed", filename="bozza.txt", text="Storica", legacy=True)
    create_run(status="prepared", dry_run=True)

    class EndScan(Exception):
        pass

    def end_scan(_):
        raise EndScan

    def unavailable(*args, **kwargs):
        raise URLError("servizio temporaneamente indisponibile")

    def scan_once(root=archive, *, model=None):
        with pytest.raises(EndScan):
            spark_archive.run_worker(
                root, output, base_url="http://reference", token="private", model=model
            )

    monkeypatch.setattr(spark_archive, "sleep", end_scan)
    monkeypatch.setattr(spark_archive, "urlopen", unavailable)
    scan_once()
    first = archive / "TEST-CASO-MIMIC-1"
    assert set(archive.iterdir()) == {first}
    assert spark_archive.read_json(first / "metriche.json")["token_e_stream"] == metrics
    assert spark_archive.read_json(first / "scheda.json")["reference"] == {"status": "pending"}

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
        "risultati_tool.json",
        "scheda.json",
        "spark.json",
    }
    sheet = spark_archive.read_json(first / "scheda.json")
    assert sheet["run_id"] == completed
    assert sheet["model"] == config.model
    assert sheet["reasoning_enabled"] is False and sheet["reasoning_effort"] is None
    assert sheet["tools_enabled"] is True
    assert sheet["artifact_files"] == ["bozza.txt", "evidenze.md", "risultati_tool.json"]
    assert spark_archive.read_json(first / "risultati_tool.json") == {
        "run_id": completed,
        "results": tool_output,
    }
    assert sheet["reference"]["status"] == "saved"
    assert (first / "bozza.txt").read_text() == "Bozza di prova"
    report = spark_archive.read_json(first / "metriche.json")
    assert report["prompt_version"] == "fixture-prompt"
    assert report["token_e_stream"] == metrics
    assert "Fonte sintetica" not in json.dumps(report)
    assert report["hardware_spark"]["samples_in_run"] == [sample]

    create_run(status="running")
    scan_once()
    scan_once()
    assert set(archive.iterdir()) == {first}
    assert (first / "lettera_ufficiale.txt").read_text() == "Originale"

    another = create_run(status="completed", filename="bozza.txt", text="Seconda bozza")
    scan_once()
    second = archive / "TEST-CASO-MIMIC-2"
    assert set(archive.iterdir()) == {first, second}
    assert (second / "bozza.txt").read_text() == "Seconda bozza"
    assert not (second / "risultati_tool.json").exists()
    sheet = spark_archive.read_json(second / "scheda.json")
    assert sheet["run_id"] == another and sheet["status"] == "completed"
    assert sheet["reference"]["note_id"] == "note-1"

    def missing_reference(request, *, timeout):
        raise HTTPError(request.full_url, 404, "Originale assente", None, None)

    failed = create_run(
        status="failed", filename="risposta.parziale.json", text="Risposta parziale"
    )
    monkeypatch.setattr(spark_archive, "urlopen", missing_reference)
    scan_once()
    third = archive / "TEST-CASO-MIMIC-3"
    assert set(archive.iterdir()) == {first, second, third}
    failed_sheet = spark_archive.read_json(third / "scheda.json")
    assert failed_sheet["run_id"] == failed
    assert failed_sheet["draft_file"] is None
    assert failed_sheet["artifact_files"] == ["risposta.parziale.json"]
    assert failed_sheet["reference"] == {"status": "unavailable", "http_status": 404}
    failed_metrics = spark_archive.read_json(third / "metriche.json")
    assert failed_metrics["status"] == "failed"
    assert failed_metrics["token_e_stream"] == metrics
    assert failed_metrics["hardware_spark"]["samples_in_run"] == [sample]

    legacy = archive / "TEST-CASO-MIMIC-4"
    legacy.mkdir()
    sheet.pop("archive_version")
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
    rmtree(third)
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

    for mode in (False, True, False):
        create_run(
            status="completed",
            filename="bozza.txt",
            text="Prova con nome",
            display_name="Caso ED 1",
            run_config=ModelConfig(thinking=mode, tools_enabled=mode),
            name_in_report=mode,
        )
        scan_once()

    names = {
        "TEST-MIMIC-CASO-ED-1-BASE",
        "TEST-MIMIC-CASO-ED-1-REASONING-TOOL",
        "TEST-MIMIC-CASO-ED-1-BASE-R2",
    }
    scan_once()
    assert {path.name for path in archive.iterdir() if path.is_dir()} == names | {first.name}
    for name in names:
        assert (
            spark_archive.read_json(archive / name / "scheda.json")["display_name"] == "Caso ED 1"
        )

    create_run(status="interrupted", with_artifacts=False, display_name="Caso ED 2")
    create_run(
        status="completed",
        filename="bozza.txt",
        text="Senza fonti",
        evidence=False,
        display_name="Caso ED 3",
    )
    scan_once()
    interrupted = archive / "TEST-MIMIC-CASO-ED-2-TOOL"
    assert {file.name for file in interrupted.iterdir()} == {
        "lettera_ufficiale.txt",
        "metriche.json",
        "scheda.json",
        "spark.json",
    }
    assert spark_archive.read_json(interrupted / "metriche.json")["status"] == "interrupted"
    without_evidence = archive / "TEST-MIMIC-CASO-ED-3-TOOL"
    assert spark_archive.read_json(without_evidence / "scheda.json")["artifact_files"] == [
        "bozza.txt"
    ]

    gemma = create_run(
        status="failed", display_name="Caso ED 1", run_config=ModelConfig(model=GEMMA_MODEL)
    )
    gemma_archive = tmp_path / "TEST-SPARK-GEMMA"
    scan_once(gemma_archive, model=GEMMA_MODEL)
    folders = list(gemma_archive.iterdir())
    assert len(folders) == 1
    assert spark_archive.read_json(folders[0] / "scheda.json")["run_id"] == gemma
