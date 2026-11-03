"""Campioni della sonda remota correlati all'intervallo della richiesta LLM.

Le risorse sono dell'host Spark condiviso: non sono attribuite esclusivamente
alla lettera. Nessun costo o consumo energetico viene dedotto dai token.
"""

import asyncio
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from codice_tesi.observability.hardware_probe import METRICS


class HardwareConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HARDWARE_", env_file="observability/hardware.env", extra="forbid"
    )
    enabled: bool = False
    endpoint: AnyHttpUrl = AnyHttpUrl("http://spark-413e.tail9c981a.ts.net:9401")
    sample_interval_seconds: float = Field(default=1, ge=0.2, le=60, allow_inf_nan=False)
    timeout_seconds: float = Field(default=1, gt=0, le=5, allow_inf_nan=False)
    max_sample_age_seconds: float = Field(default=10, ge=1, le=120, allow_inf_nan=False)

    @field_validator("endpoint")
    @classmethod
    def no_credentials(cls, url):
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("L'endpoint della sonda non deve contenere credenziali o query.")
        return url


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_snapshot(config):
    request = Request(
        str(config.endpoint).rstrip("/") + "/snapshot", headers={"Accept": "application/json"}
    )
    with build_opener(NoRedirects).open(request, timeout=config.timeout_seconds) as response:
        data = response.read(262145)
    if len(data) > 262144:
        raise ValueError("Snapshot troppo grande")
    return json.loads(data)


def validate_snapshot(snapshot, *, max_age, now=None):
    """Conserva solo il contratto hardware, anche nei campioni grezzi locali."""
    now = now or datetime.now(UTC)
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "1.0":
        raise ValueError("Schema hardware non supportato")
    sampled_at = datetime.fromisoformat(snapshot["sampled_at"])
    if sampled_at.tzinfo is None:
        raise ValueError("Timestamp privo di timezone")
    age = (now - sampled_at).total_seconds()
    if age > max_age or age < -5:
        raise ValueError("Campione scaduto o orologi non sincronizzati")
    if not re.fullmatch(r"[a-f0-9]{32}", snapshot.get("boot_id", "")):
        raise ValueError("Identità della sonda non valida")
    sequence = snapshot.get("sequence")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("Sequenza non valida")
    host = snapshot.get("host", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", host):
        raise ValueError("Host non valido")
    metrics = []
    seen = set()
    for item in snapshot.get("metrics", []):
        name, value, labels = item["name"], item["value"], item.get("labels", {})
        if name not in METRICS:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Valore hardware non valido")
        gpu = name.startswith("spark_gpu_")
        if (gpu and (set(labels) != {"gpu"} or not re.fullmatch(r"\d{1,3}", labels["gpu"]))) or (
            not gpu and labels
        ):
            raise ValueError("Etichette hardware non valide")
        if name.endswith("_ratio") and value > 1:
            raise ValueError("Rapporto hardware fuori intervallo")
        key = (name, tuple(sorted(labels.items())))
        if key in seen:
            raise ValueError("Serie duplicata nello snapshot")
        seen.add(key)
        metrics.append({"name": name, "labels": dict(labels), "value": value})
    sources = {}
    for source in ("cpu", "memory", "disk", "gpu"):
        raw = snapshot.get("sources", {}).get(source, {})
        sources[source] = {"available": raw.get("available") is True}
        error_type = raw.get("error_type", "")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", error_type):
            sources[source]["error_type"] = error_type
    identity = {}
    for key in ("architecture", "driver_version"):
        value = snapshot.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
            identity[key] = value
    devices = []
    for device in snapshot.get("devices", []):
        model, gpu = device.get("model"), device.get("gpu")
        if (
            isinstance(model, str)
            and re.fullmatch(r"[A-Za-z0-9 ()_.+-]{1,160}", model)
            and isinstance(gpu, str)
            and re.fullmatch(r"\d{1,3}", gpu)
        ):
            devices.append({"gpu": gpu, "model": model})
    return {
        "schema_version": "1.0",
        "boot_id": snapshot["boot_id"],
        "sequence": sequence,
        "sampled_at": sampled_at.astimezone(UTC).isoformat(),
        "host": host,
        **identity,
        "devices": devices,
        "sources": sources,
        "metrics": metrics,
    }


class HardwareObservation:
    def __init__(self, config, output_dir, run_id):
        self.config = config
        self.path = Path(output_dir).resolve() / "hardware" / f"{run_id}.jsonl"
        self.task = None
        self.stop_event = asyncio.Event()
        self.started_at = self.ended_at = None
        self.started = self.ended = None
        self.samples = self.errors = self.duplicates = 0
        self.outside_window = 0
        self.last_error_type = None
        self.first_sample_at = self.last_sample_at = self.host = None
        self.sources = {}
        self.device_metadata = {}
        self.series = {}
        self.seen = set()
        self.identity = None
        self.stream = None

    def start(self):
        if self.config.enabled:
            self.started_at = datetime.now(UTC).isoformat()
            self.started = monotonic()
            self.task = asyncio.create_task(self._collect())

    async def _collect(self):
        while not self.stop_event.is_set():
            try:
                raw = await asyncio.to_thread(fetch_snapshot, self.config)
                if self.stop_event.is_set():
                    break
                snapshot = validate_snapshot(raw, max_age=self.config.max_sample_age_seconds)
                self._observe(snapshot)
            except Exception as error:
                self.errors += 1
                self.last_error_type = type(error).__name__
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=self.config.sample_interval_seconds
                )
            except TimeoutError:
                pass

    def _observe(self, snapshot):
        if datetime.fromisoformat(snapshot["sampled_at"]) < datetime.fromisoformat(self.started_at):
            self.outside_window += 1
            return
        # Non mescolare host o riavvii della sonda nella stessa statistica.
        identity = snapshot["host"], snapshot["boot_id"]
        if self.identity is not None and identity != self.identity:
            raise ValueError("La sonda è cambiata durante la richiesta")
        self.identity = identity
        key = snapshot["boot_id"], snapshot["sequence"]
        if key in self.seen:
            self.duplicates += 1
        else:
            self._accept(snapshot, key)

    def _accept(self, snapshot, key):
        if self.stream is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = self.path.open("x", encoding="utf-8")
        self.stream.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        self.stream.flush()
        self.seen.add(key)
        self.samples += 1
        self.host, self.sources = snapshot["host"], snapshot["sources"]
        self.device_metadata = {
            key: snapshot.get(key) for key in ("architecture", "driver_version", "devices")
        }
        self.first_sample_at = self.first_sample_at or snapshot["sampled_at"]
        self.last_sample_at = snapshot["sampled_at"]
        for item in snapshot["metrics"]:
            key = item["name"], tuple(sorted(item["labels"].items()))
            if key not in self.series:
                self.series[key] = {"count": 0, "sum": 0, "min": item["value"], "max": 0}
            series = self.series[key]
            series["count"] += 1
            series["sum"] += item["value"]
            series["min"] = min(series["min"], item["value"])
            series["max"] = max(series["max"], item["value"])

    async def stop(self):
        try:
            if self.task is not None:
                self.ended_at = datetime.now(UTC).isoformat()
                self.ended = monotonic()
                self.stop_event.set()
                await self.task
        except Exception as error:
            self.errors += 1
            self.last_error_type = type(error).__name__
        finally:
            if self.stream is not None:
                try:
                    self.stream.close()
                except OSError as error:
                    self.errors += 1
                    self.last_error_type = type(error).__name__

    def summary(self):
        if not self.config.enabled:
            status = "disabled"
        elif self.started is None:
            status = "not_started"
        elif not self.series:
            status = "unavailable" if self.errors or self.samples else "no_samples"
        elif self.errors or any(not source["available"] for source in self.sources.values()):
            status = "partial"
        else:
            status = "available"
        return {
            "schema_version": "1.0",
            "enabled": self.config.enabled,
            "status": status,
            "scope": "spark_host_during_agent_run",
            "attribution": "shared_host_not_exclusive_to_request",
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "observation_seconds": (
                round((self.ended or monotonic()) - self.started, 6)
                if self.started is not None
                else None
            ),
            "host": self.host,
            **self.device_metadata,
            "samples": self.samples,
            "errors": self.errors,
            "duplicate_samples": self.duplicates,
            "outside_window_samples": self.outside_window,
            "last_error_type": self.last_error_type,
            "first_sample_at": self.first_sample_at,
            "last_sample_at": self.last_sample_at,
            "sources": self.sources,
            "raw_samples_path": str(self.path) if self.samples else None,
            "statistics": [
                {
                    "name": name,
                    "labels": dict(labels),
                    "unit": METRICS[name][1],
                    "samples": stats["count"],
                    "min": stats["min"],
                    "max": stats["max"],
                    "mean": stats["sum"] / stats["count"],
                }
                for (name, labels), stats in sorted(self.series.items())
            ],
            "mean_method": "arithmetic_mean_of_unique_samples_not_time_weighted",
        }

    def span_attributes(self):
        summary = self.summary()
        attributes = {
            "tesi.hardware.status": summary["status"],
            "tesi.hardware.samples": self.samples,
            "tesi.hardware.scope": summary["scope"],
            "tesi.hardware.attribution": summary["attribution"],
            "tesi.hardware.statistics": json.dumps(summary["statistics"], sort_keys=True),
        }
        if self.host:
            attributes["tesi.hardware.host"] = self.host
        return attributes
