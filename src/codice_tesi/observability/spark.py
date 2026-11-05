"""Acquisizione locale delle misure hardware dello Spark via Tailscale."""

import json
from datetime import UTC, datetime
from http.client import HTTPException
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

SPARK_MONITOR_URL = "http://spark-413e.tail9c981a.ts.net:9108"
SPARK_HOSTS = {"spark-413e", urlsplit(SPARK_MONITOR_URL).hostname}


def run_interval(record):
    fields = ("run_id", "started_at", "ended_at")
    if not isinstance(record, dict) or not all(
        isinstance(record.get(key), str) and record[key] for key in fields
    ):
        raise ValueError("Serve il record di un'esecuzione conclusa")
    start = datetime.fromisoformat(record["started_at"])
    end = datetime.fromisoformat(record["ended_at"])
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("Intervallo del registro non valido o privo di fuso orario")
    return start.astimezone(UTC), end.astimezone(UTC)


def capture_snapshot(base_url, hours, *, record=None, timeout=15):
    query = urlencode({"hours": hours})
    snapshot = {"base_url": base_url, "requested_hours": hours, "errors": {}}
    for endpoint in ("history", "energy"):
        try:
            url = f"{base_url.rstrip('/')}/{endpoint}?{query}"
            with urlopen(url, timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("Risposta senza un oggetto JSON")
            if endpoint == "history" and not isinstance(payload.get("series"), list):
                raise ValueError("Risposta senza la serie dei campioni")
            snapshot[endpoint] = payload
        except (HTTPException, OSError, ValueError) as exc:
            snapshot[endpoint] = None
            snapshot["errors"][endpoint] = type(exc).__name__
    snapshot["captured_at"] = datetime.now(UTC).isoformat()

    if record is not None:
        start, end = run_interval(record)
        lower, upper = start.timestamp(), end.timestamp()
        series = (snapshot["history"] or {}).get("series", [])
        if not all(
            isinstance(sample, dict)
            and type(sample.get("t")) in (int, float)
            and isfinite(sample["t"])
            for sample in series
        ):
            snapshot["errors"]["history"] = "InvalidSampleTimestamp"
            series = []
        snapshot["run"] = {
            "run_id": record["run_id"],
            "started_at": start.isoformat(),
            "ended_at": end.isoformat(),
            "series": [sample for sample in series if lower <= sample["t"] <= upper],
        }
    return snapshot


def save_snapshot(destination: Path, snapshot):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, delete=False
    ) as temporary:
        path = Path(temporary.name)
        try:
            json.dump(snapshot, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.close()
            path.replace(destination)
        finally:
            path.unlink(missing_ok=True)


def capture_run(output_dir: Path, record):
    start, _ = run_interval(record)
    hours = max(0.25, ((datetime.now(UTC) - start).total_seconds() + 60) / 3600)
    snapshot = capture_snapshot(SPARK_MONITOR_URL, hours, record=record, timeout=2)
    relative_path = Path("spark") / f"{record['run_id']}.json"
    save_snapshot(output_dir / relative_path, snapshot)
    count = len(snapshot["run"]["series"])
    errors = snapshot["errors"]
    if len(errors) == 2:
        status = "unavailable"
    elif errors:
        status = "partial"
    else:
        status = "captured" if count else "no_samples"
    return {
        "status": status,
        "path": relative_path.as_posix(),
        "sample_count": None if "history" in errors else count,
    }
