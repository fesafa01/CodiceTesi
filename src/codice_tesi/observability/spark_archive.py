"""Archivio locale delle prove, eseguito sulla rete privata di revisione."""

import json
import logging
import os
import re
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from time import sleep
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from codice_tesi.observability.spark import save_snapshot

PREFIX = "TEST-CASO-MIMIC-"
SHEET = "scheda.json"
RUN_FILES = (
    "richiesta.txt",
    "catalogo_evidenze.json",
    "fatti_documentali.json",
    "risposta.parziale.json",
    "bozza_strutturata.json",
    "evidenze.md",
    "bozza.txt",
    "esito.json",
)
logger = logging.getLogger(__name__)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def archive_run(destination: Path, record_path: Path, record: dict) -> dict:
    output = record_path.parents[1]
    run = record["test_spark"]
    config = record["model_config"]
    metrics = record["llm"]
    hardware = record.get("spark_monitor", {"status": "not_collected"})
    snapshot = read_json(output / hardware["path"]) if hardware.get("path") else {}
    sheet = {
        "run_id": record["run_id"],
        "context_id": run["context_id"],
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
        "draft_file": None,
        "artifact_files": [],
        "reference": {"status": "pending"},
    }
    with TemporaryDirectory(prefix=".prova-", dir=destination.parent) as staging:
        staging = Path(staging)
        if run["artifact_directory"]:
            artifacts = output / run["artifact_directory"]
            for name in RUN_FILES:
                source = artifacts / name
                if source.is_file():
                    copyfile(source, staging / name)
                    sheet["artifact_files"].append(name)
            for name in ("bozza.txt", "risposta.parziale.json"):
                copied = staging / name
                if copied.is_file() and copied.stat().st_size:
                    sheet["draft_file"] = name
                    break
        save_snapshot(staging / "registro.json", record)
        save_snapshot(
            staging / "metriche.json",
            {
                "run_id": record["run_id"],
                "token_e_stream": metrics,
                "input_tokens_estimate": record.get("input_tokens_estimate"),
                "timings": record["timings"],
                "trace_id": record["trace_id"],
                "telemetry": record["telemetry"],
                "hardware_spark": {
                    "status": hardware["status"],
                    "sample_count": hardware.get("sample_count"),
                    "errors": snapshot.get("errors", {}),
                    "error_type": hardware.get("error_type"),
                    "samples_in_run": snapshot.get("run", {}).get("series"),
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


def run_worker(root: Path, output: Path, *, base_url: str, token: str) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    archived = set()
    pending = {}
    number = 0
    for directory in root.glob(f"{PREFIX}*"):
        if not re.fullmatch(rf"{PREFIX}[0-9]+", directory.name):
            continue
        number = max(number, int(directory.name.removeprefix(PREFIX)))
        sheet = read_json(directory / SHEET)
        archived.add(sheet["run_id"])
        if sheet["reference"]["status"] == "pending":
            pending[directory] = sheet

    while True:
        for path in sorted(output.glob("dimissioni*/registro/*.json")):
            if path.stem in archived:
                continue
            try:
                record = read_json(path)
                if not record.get("test_spark"):
                    continue
                destination = root / f"{PREFIX}{number + 1}"
                sheet = archive_run(destination, path, record)
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("Scrittura archivio da riprovare (%s)", type(exc).__name__)
                continue
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
        )
