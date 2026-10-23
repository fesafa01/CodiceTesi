"""Registro atomico per tentativo, separato da bozze e dashboard."""

import csv
import hashlib
import json
import platform
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

REGISTRY_SCHEMA = "1.0"


class ExperimentConfig(BaseModel):
    """Metadati dichiarati dall'operatore; null significa non attestato."""

    model_config = ConfigDict(extra="forbid")
    experiment_id: str = Field(default="exploratory", min_length=1, max_length=120)
    condition: str | None = Field(default=None, max_length=120)
    repetition: int | None = Field(default=None, ge=1)
    model_revision: str | None = None
    tokenizer_revision: str | None = None
    quantization: str | None = None
    serving_runtime: str | None = None
    serving_version: str | None = None
    inference_hardware: str | None = None
    cache_state: str = "unknown"
    concurrency: int | None = Field(default=None, ge=1)
    dataset_split: str | None = None


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source_files(root):
    """Nell'installazione usa RECORD, evitando copie residue non distribuite."""
    try:
        package = distribution("codice-tesi")
        installed_root = Path(package.locate_file("codice_tesi"))
        if installed_root.resolve() == root.resolve() and package.files is not None:
            return sorted(
                Path(package.locate_file(file))
                for file in package.files
                if str(file).startswith("codice_tesi/") and file.suffix in {".py", ".json", ".sql"}
            )
    except PackageNotFoundError:
        pass
    return sorted(
        file
        for file in root.rglob("*")
        if file.suffix in {".py", ".json", ".sql"}
        and all(" " not in part for part in file.relative_to(root).parts)
    )


def software_manifest():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for file in _source_files(root):
        digest.update(str(file.relative_to(root)).encode() + b"\0" + file.read_bytes())
    packages = {}
    for name in (
        "codice-tesi",
        "pydantic-ai-slim",
        "pydantic",
        "openai",
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-http",
    ):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {
        "source_sha256": digest.hexdigest(),
        "packages": packages,
        "python": sys.version.split()[0],
        "client_platform": platform.system(),
        "client_architecture": platform.machine(),
    }


class ExecutionRecord:
    def __init__(self, output_dir, *, experiment, config, context_id, dry_run):
        self.started = monotonic()
        self.path = Path(output_dir).resolve() / "registro" / f"{uuid4().hex}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {
            "schema_version": REGISTRY_SCHEMA,
            "run_id": self.path.stem,
            "started_at": datetime.now(UTC).isoformat(),
            "ended_at": None,
            "status": "preparing",
            "dry_run": dry_run,
            "experiment": experiment.model_dump(mode="json"),
            "experiment_metadata_source": "operator_declared",
            "requested_context_id": context_id,
            "model_config": config.model_dump(mode="json"),
            "request_settings": config.request_settings(),
            "request_settings_sha256": canonical_hash(config.request_settings()),
            "software": software_manifest(),
            "timings": {},
            "llm": None,
            "artifact_directory": None,
            "trace_id": None,
            "root_span_id": None,
            "error_type": None,
            "failure_stage": None,
        }
        self.save()

    def bind(self, packet, request, artifacts, context_config):
        self.data.update(
            {
                "context_id": packet.context.id,
                "dataset": packet.context.dataset,
                "dataset_version": packet.context.dataset_version,
                "packet_sha256": canonical_hash(packet.model_dump(mode="json")),
                "request_sha256": canonical_hash(
                    {
                        "instructions": request.instructions,
                        "prompt": request.prompt,
                    }
                ),
                "template_sha256": packet.template.sha256,
                "template_version": packet.template.version,
                "prompt_version": artifacts.report["prompt_version"],
                "source_count": len(packet.sources),
                "input_tokens_estimate": request.input_tokens_estimate,
                "input_estimate_method": "utf8_json_bytes_plus_512_not_tokenizer_count",
                "context_config": context_config.model_dump(mode="json"),
                "artifact_directory": str(artifacts.directory),
            }
        )
        artifacts.report["run_id"] = self.data["run_id"]
        artifacts.report["trace_id"] = self.data["trace_id"]
        artifacts.report["registro_schema"] = REGISTRY_SCHEMA
        artifacts.finish("preparato")
        self.save()

    def save(self):
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.path)

    def finish(self, status, *, error=None, llm=None):
        self.data["status"] = status
        self.data["ended_at"] = datetime.now(UTC).isoformat()
        self.data["timings"]["total_seconds"] = round(monotonic() - self.started, 6)
        self.data["error_type"] = type(error).__name__ if error else None
        if llm is not None:
            self.data["llm"] = llm
        self.save()


def _flatten(value, prefix=""):
    result = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            result.update(_flatten(item, name))
        elif isinstance(item, list):
            result[name] = json.dumps(item, ensure_ascii=False)
        else:
            result[name] = item
    return result


def export_registry(output_dir: Path, destination: Path, *, format: str):
    files = sorted((output_dir / "registro").glob("*.json"))
    if destination.resolve() in {f.resolve() for f in files}:
        raise ValueError("L'esportazione non può sovrascrivere un record del registro.")
    rows = [json.loads(file.read_text()) for file in files]
    for row in rows:
        if row.get("schema_version") != REGISTRY_SCHEMA:
            raise ValueError("Versione del registro non supportata; nessun file esportato.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        if format == "jsonl":
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            flat = [_flatten(row) for row in rows]
            keys = sorted({key for row in flat for key in row})
            writer = csv.DictWriter(stream, fieldnames=keys)
            writer.writeheader()
            writer.writerows(flat)
    temporary.replace(destination)
    return len(rows)
