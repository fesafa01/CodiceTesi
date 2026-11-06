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
RESET_MARKER = ".archive-reset"
ARCHIVE_VERSION = 2
logger = logging.getLogger(__name__)


class ReferenceUnavailable(Exception):
    """Il servizio di revisione non ha un originale per la versione del caso."""


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def archive_run(
    destination: Path, record_path: Path, record: dict, *, base_url: str, token: str
) -> dict | None:
    run = record.get("test_spark") or {}
    if (
        run.get("archive_version") != ARCHIVE_VERSION
        or record.get("status") != "completed"
        or not run.get("artifact_directory")
    ):
        return None
    output = record_path.parents[1]
    artifacts = output / run["artifact_directory"]
    draft = artifacts / "bozza.txt"
    if not draft.is_file() or not draft.stat().st_size:
        return None
    evidence = artifacts / "evidenze.md"
    if not evidence.is_file() or not evidence.stat().st_size:
        return None
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
        "draft_file": draft.name,
        "draft_technical_validation": record.get("draft_technical_validation"),
        "artifact_files": [draft.name, evidence.name],
        "reference": {"status": "pending"},
    }
    with TemporaryDirectory(prefix=".prova-", dir=destination.parent) as staging:
        staging = Path(staging)
        copyfile(draft, staging / draft.name)
        copyfile(evidence, staging / evidence.name)
        save_snapshot(
            staging / "metriche.json",
            {
                "run_id": record["run_id"],
                "prompt_version": record.get("prompt_version"),
                "draft_technical_validation": record.get("draft_technical_validation"),
                "token_e_stream": metrics,
                "input_tokens_estimate": record.get("input_tokens_estimate"),
                "initial_evidence_count": record.get("initial_evidence_count"),
                "evidence_count": record.get("evidence_count"),
                "timings": record["timings"],
                "trace_id": record["trace_id"],
                "telemetry": record["telemetry"],
                "hardware_spark": {
                    "status": hardware["status"],
                    "sample_count": hardware.get("sample_count"),
                    "errors": snapshot.get("errors", {}),
                    "error_type": hardware.get("error_type"),
                    "samples_in_run": snapshot.get("run", {}).get("series"),
                    "energy_in_run": snapshot.get("run", {}).get("energy"),
                    "energy_monitor_window": snapshot.get("energy"),
                    "monitor_window_hours": snapshot.get("requested_hours"),
                },
            },
        )
        archive_reference(staging, sheet, base_url=base_url, token=token)
        if sheet["reference"]["status"] != "saved":
            raise ReferenceUnavailable
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
    try:
        reset_at = (root / RESET_MARKER).stat().st_mtime_ns
    except FileNotFoundError:
        reset_at = None
    archived = set()
    unavailable = set()
    pending = {}
    number = 0
    for directory in root.glob(f"{PREFIX}*"):
        if not re.fullmatch(rf"{PREFIX}[0-9]+", directory.name):
            continue
        number = max(number, int(directory.name.removeprefix(PREFIX)))
        try:
            sheet = read_json(directory / SHEET)
            archived.add(sheet["run_id"])
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Scheda storica non leggibile; numero di archivio riservato")
            continue
        if (
            sheet.get("status") == "completed"
            and sheet.get("draft_file") == "bozza.txt"
            and (sheet.get("reference") or {}).get("status") == "pending"
        ):
            pending[directory] = sheet

    while True:
        for path in sorted(output.glob("dimissioni*/registro/*.json")):
            if path.stem in archived or path.stem in unavailable:
                continue
            try:
                if reset_at is not None and path.stat().st_mtime_ns <= reset_at:
                    continue
                record = read_json(path)
                destination = root / f"{PREFIX}{number + 1}"
                sheet = archive_run(destination, path, record, base_url=base_url, token=token)
            except ReferenceUnavailable:
                unavailable.add(path.stem)
                logger.warning("Originale indisponibile (HTTP 404); prova non pubblicata")
                continue
            except (OSError, ValueError, KeyError, AttributeError) as exc:
                logger.warning("Scrittura archivio da riprovare (%s)", type(exc).__name__)
                continue
            if sheet is None:
                continue
            number += 1
            archived.add(sheet["run_id"])
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
