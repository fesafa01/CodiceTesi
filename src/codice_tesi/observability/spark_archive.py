"""Archivio locale delle prove, eseguito sulla rete privata di revisione."""

import json
import logging
import os
import re
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from time import sleep
from unicodedata import normalize
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from codice_tesi.discharge.contracts import TOOL_RESULTS_FILE
from codice_tesi.observability.spark import save_snapshot

LEGACY_PREFIX = "TEST-CASO-MIMIC-"
CASE_PREFIX = "TEST-MIMIC-"
SHEET = "scheda.json"
RESET_MARKER = ".archive-reset"
ARCHIVE_VERSION = 2
TERMINAL_STATUSES = {"completed", "failed", "interrupted"}
logger = logging.getLogger(__name__)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _archivable_run(record):
    run = record.get("test_spark") or {}
    if run.get("archive_version") == ARCHIVE_VERSION and record.get("status") in TERMINAL_STATUSES:
        return run
    return None


def _display_name(record_path, run):
    if "display_name" in run or not run.get("artifact_directory"):
        return run.get("display_name")
    # I run precedenti alla modifica hanno già il nome nel rapporto di esecuzione.
    report = record_path.parents[1] / run["artifact_directory"] / "esito.json"
    try:
        return read_json(report).get("contesto", {}).get("display_name")
    except (OSError, ValueError, AttributeError):
        return None


def _archive_destination(root, record_path, record, run, number):
    name = _display_name(record_path, run)
    if isinstance(name, str):
        run["display_name"] = name
        normalized = normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
        slug = re.sub(r"[^A-Z0-9]+", "-", normalized).strip("-")[:80].rstrip("-")
        if slug:
            config = record["model_config"]
            modes = {
                (False, False): "BASE",
                (True, False): "REASONING",
                (False, True): "TOOL",
                (True, True): "REASONING-TOOL",
            }
            mode = modes[config["thinking"], config["tools_enabled"]]
            stem = f"{CASE_PREFIX}{slug}-{mode}"
            destination = root / stem
            repetition = 2
            while destination.exists() or destination.is_symlink():
                destination = root / f"{stem}-R{repetition}"
                repetition += 1
            return destination
    return root / f"{LEGACY_PREFIX}{number + 1}"


def archive_run(destination: Path, record_path: Path, record: dict) -> dict | None:
    run = _archivable_run(record)
    if run is None:
        return None
    output = record_path.parents[1]
    artifact_files = []
    if directory := run.get("artifact_directory"):
        artifacts = output / directory
        draft_name = "bozza.txt" if record["status"] == "completed" else "risposta.parziale.json"
        candidates = [artifacts / draft_name, artifacts / "evidenze.md"]
        if TOOL_RESULTS_FILE in (record.get("artifact_hashes") or {}):
            candidates.append(artifacts / TOOL_RESULTS_FILE)
        artifact_files = [path for path in candidates if path.is_file() and path.stat().st_size]
    config = record["model_config"]
    metrics = record["llm"]
    hardware = record.get("spark_monitor", {"status": "not_collected"})
    snapshot = {}
    if hardware.get("path"):
        try:
            snapshot = read_json(output / hardware["path"])
            if not isinstance(snapshot, dict):
                raise ValueError("Snapshot hardware non valido")
        except (OSError, ValueError) as exc:
            snapshot = {}
            hardware = {"status": "unavailable", "error_type": type(exc).__name__}
    artifact_names = [path.name for path in artifact_files]
    snapshot_file = "spark.json" if snapshot else None
    sheet = {
        "run_id": record["run_id"],
        "archive_version": ARCHIVE_VERSION,
        "context_id": run["context_id"],
        "display_name": run.get("display_name"),
        "dataset_version": run["dataset_version"],
        "status": record["status"],
        "error_type": record["error_type"],
        "started_at": record["started_at"],
        "ended_at": record["ended_at"],
        "model": config["model"],
        "response_model": (metrics or {}).get("response_model"),
        "reasoning_enabled": config["thinking"],
        "reasoning_effort": (
            record["request_settings"].get("openai_reasoning_effort")
            if config["thinking"]
            else None
        ),
        "tools_enabled": config["tools_enabled"],
        "draft_file": "bozza.txt" if "bozza.txt" in artifact_names else None,
        "draft_technical_validation": record.get("draft_technical_validation"),
        "technical_first_pass": record.get("technical_first_pass"),
        "deterministic_repair": record.get("deterministic_repair"),
        "artifact_files": artifact_names,
        "hardware_snapshot_file": snapshot_file,
        "reference": {"status": "pending"},
    }
    with TemporaryDirectory(prefix=".prova-", dir=destination.parent) as staging:
        staging = Path(staging)
        for path in artifact_files:
            copyfile(path, staging / path.name)
        if snapshot_file:
            save_snapshot(staging / snapshot_file, snapshot)
        save_snapshot(
            staging / "metriche.json",
            {
                "run_id": record["run_id"],
                "status": record["status"],
                "error_type": record["error_type"],
                "prompt_version": record.get("prompt_version"),
                "draft_technical_validation": record.get("draft_technical_validation"),
                "token_e_stream": metrics,
                "input_tokens_estimate": record.get("input_tokens_estimate"),
                "input_tokens_breakdown": record.get("input_tokens_breakdown"),
                "input_estimate_method": record.get("input_estimate_method"),
                "input_tokenizer_revision": record.get("input_tokenizer_revision"),
                "initial_evidence_count": record.get("initial_evidence_count"),
                "evidence_count": record.get("evidence_count"),
                "timings": record["timings"],
                "model_config": config,
                "request_settings": record.get("request_settings"),
                "software": record.get("software"),
                "failure_stage": record.get("failure_stage"),
                "trace_id": record["trace_id"],
                "telemetry": record["telemetry"],
                "hardware_spark": {
                    "status": hardware["status"],
                    "sample_count": hardware.get("sample_count"),
                    "errors": snapshot.get("errors", {}),
                    "error_type": hardware.get("error_type"),
                    "samples_in_run": snapshot.get("run", {}).get("series"),
                    "energy_in_run": snapshot.get("run", {}).get("energy"),
                    "energy_by_source_in_run": snapshot.get("run", {}).get("energy_by_source"),
                    "summary_in_run": snapshot.get("run", {}).get("summary"),
                    "energy_monitor_window": snapshot.get("energy"),
                    "monitor_window_hours": snapshot.get("requested_hours"),
                },
            },
        )
        save_snapshot(staging / SHEET, sheet)
        staging.rename(destination)
    return sheet


def archive_reference(directory: Path, sheet: dict, *, base_url: str, token: str) -> None:
    context = quote(sheet["context_id"], safe="")
    query = urlencode({"versione": sheet["dataset_version"]})
    request = Request(
        f"{base_url.rstrip('/')}/api/v1/revisione/contesti/{context}/lettera-originale?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            note = json.load(response)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        reference = {"status": "unavailable", "http_status": 404}
    else:
        if not all(isinstance(note.get(key), str) and note[key] for key in ("note_id", "text")):
            raise ValueError("Risposta del servizio di revisione non valida.")
        filename = "lettera_ufficiale.txt"
        # Se il processo si ferma, la scheda rimane pending e la copia viene riscritta.
        temporary = directory / ".lettera.tmp"
        temporary.write_text(note["text"], encoding="utf-8")
        temporary.replace(directory / filename)
        reference = {"status": "saved", "note_id": note["note_id"], "file": filename}
    save_snapshot(directory / SHEET, {**sheet, "reference": reference})
    sheet["reference"] = reference


def run_worker(
    root: Path, output: Path, *, base_url: str, token: str, model: str | None = None
) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        reset_at = (root / RESET_MARKER).stat().st_mtime_ns
    except FileNotFoundError:
        reset_at = None
    archived = set()
    pending = {}
    number = 0
    for directory in root.iterdir():
        legacy = re.fullmatch(rf"{LEGACY_PREFIX}([0-9]+)", directory.name)
        if legacy is None and not re.fullmatch(rf"{CASE_PREFIX}[A-Z0-9-]+", directory.name):
            continue
        if legacy:
            number = max(number, int(legacy[1]))
        try:
            sheet = read_json(directory / SHEET)
            archived.add(sheet["run_id"])
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Scheda storica non leggibile; numero di archivio riservato")
            continue
        if model is not None and sheet.get("model") != model:
            continue
        if (
            sheet.get("archive_version") == ARCHIVE_VERSION
            or (sheet.get("status") == "completed" and sheet.get("draft_file") == "bozza.txt")
        ) and (sheet.get("reference") or {}).get("status") == "pending":
            pending[directory] = sheet

    while True:
        for path in sorted(output.glob("dimissioni*/registro/*.json")):
            if path.stem in archived:
                continue
            try:
                if reset_at is not None and path.stat().st_mtime_ns <= reset_at:
                    continue
                record = read_json(path)
                run = _archivable_run(record)
                if run is None or (model is not None and record["model_config"]["model"] != model):
                    continue
                destination = _archive_destination(root, path, record, run, number)
                sheet = archive_run(destination, path, record)
            except (OSError, ValueError, KeyError, AttributeError) as exc:
                logger.warning("Scrittura archivio da riprovare (%s)", type(exc).__name__)
                continue
            if sheet is None:
                continue
            if destination.name.startswith(LEGACY_PREFIX):
                number += 1
            archived.add(sheet["run_id"])
            pending[destination] = sheet
        for directory, sheet in list(pending.items()):
            try:
                archive_reference(directory, sheet, base_url=base_url, token=token)
            except (OSError, ValueError, AttributeError) as exc:
                logger.warning("Lettura riferimento da riprovare (%s)", type(exc).__name__)
            else:
                del pending[directory]
        sleep(2)


if __name__ == "__main__":
    token = os.getenv("APP_MIMIC_TOKEN")
    if token:
        run_worker(
            Path("TEST-SPARK"),
            Path("output"),
            base_url=os.environ["REFERENCE_API_URL"],
            token=token,
            model=os.getenv("TESI_SPARK_ARCHIVE_MODEL") or None,
        )
