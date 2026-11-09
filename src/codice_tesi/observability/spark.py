"""Acquisizione locale delle misure hardware dello Spark via Tailscale."""

import json
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime
from http.client import HTTPException
from itertools import pairwise
from math import isfinite
from pathlib import Path
from statistics import fmean
from tempfile import NamedTemporaryFile
from time import monotonic, sleep
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

SPARK_MONITOR_URL = "http://spark-413e.tail9c981a.ts.net:9108"
SPARK_HOSTS = {"spark-413e", urlsplit(SPARK_MONITOR_URL).hostname}
HISTORY_RETRY_SECONDS = 30
HISTORY_POLL_SECONDS = 2
MAX_POWER_GAP_SECONDS = 20
POWER_FIELDS = {"plug": "plug_power_w", "gpu": "gpu_power_w"}
MONITOR_ERRORS = (HTTPException, OSError, ValueError)


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


def _fetch_endpoint(base_url, endpoint, query, timeout):
    url = f"{base_url.rstrip('/')}/{endpoint}?{query}"
    with urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Risposta senza un oggetto JSON")
    if endpoint == "history" and not isinstance(payload.get("series"), list):
        raise ValueError("Risposta senza la serie dei campioni")
    return payload


def _finite_number(value):
    return type(value) in (int, float) and isfinite(value)


def _valid_timestamps(series):
    return all(isinstance(sample, dict) and _finite_number(sample.get("t")) for sample in series)


def _energy_result(*, source=None, wh=None, reason=None, max_gap=None):
    return {
        "wh": wh,
        "source": source,
        "status": "estimated" if wh is not None else "unavailable",
        "reason": reason,
        "max_sample_gap_s": max_gap,
    }


def _integrate_power(samples, source, lower, upper, gap_limit):
    timestamps = sorted(samples)
    start = bisect_right(timestamps, lower) - 1
    end = bisect_left(timestamps, upper)
    if start < 0:
        return _energy_result(source=source, reason="missing_start_sample")
    if end == len(timestamps):
        return _energy_result(source=source, reason="missing_end_sample")

    intervals = list(pairwise(timestamps[start : end + 1]))
    max_gap = max((right - left for left, right in intervals), default=0)
    if max_gap > gap_limit:
        return _energy_result(source=source, reason="sample_gap_too_large", max_gap=max_gap)

    watt_seconds = 0.0
    for left, right in intervals:
        clipped_left = max(left, lower)
        clipped_right = min(right, upper)
        slope = (samples[right] - samples[left]) / (right - left)
        first_watts = samples[left] + slope * (clipped_left - left)
        last_watts = samples[left] + slope * (clipped_right - left)
        watt_seconds += (first_watts + last_watts) / 2 * (clipped_right - clipped_left)
    return _energy_result(source=source, wh=watt_seconds / 3600, max_gap=max_gap)


def _energy_by_source(history, lower, upper):
    step = history.get("step")
    gap_limit = MAX_POWER_GAP_SECONDS
    if _finite_number(step) and step > 0:
        gap_limit = min(gap_limit, 2 * step)
    results = {}
    for source, field in POWER_FIELDS.items():
        samples = {
            sample["t"]: sample[field]
            for sample in history["series"]
            if _finite_number(sample.get(field)) and sample[field] >= 0
        }
        results[source] = (
            _integrate_power(samples, source, lower, upper, gap_limit)
            if samples
            else _energy_result(source=source, reason="no_power_samples")
        )
    return results


def _preferred_energy(results):
    result = _energy_result(reason="no_power_samples")
    for candidate in results.values():
        if candidate["status"] == "estimated":
            return candidate
        if candidate["reason"] != "no_power_samples" and result["reason"] != "missing_end_sample":
            result = candidate
    return result


def estimate_run_energy(record, history):
    """Ricalcola la stima da uno storico già salvato."""
    start, end = run_interval(record)
    if history is None:
        return _energy_result(reason="history_unavailable")
    series = history.get("series") if isinstance(history, dict) else None
    if not isinstance(series, list) or not _valid_timestamps(series):
        return _energy_result(reason="invalid_sample_timestamp")
    return _preferred_energy(_energy_by_source(history, start.timestamp(), end.timestamp()))


def _capture_run_energy(snapshot, query, timeout, lower, upper):
    history = snapshot["history"]
    if history is None:
        return [], _energy_result(reason="history_unavailable"), {}
    if not _valid_timestamps(history["series"]):
        snapshot["errors"]["history"] = "InvalidSampleTimestamp"
        return [], _energy_result(reason="invalid_sample_timestamp"), {}

    results = _energy_by_source(history, lower, upper)
    deadline = monotonic() + HISTORY_RETRY_SECONDS
    while any(result["reason"] == "missing_end_sample" for result in results.values()):
        remaining = deadline - monotonic()
        if remaining <= HISTORY_POLL_SECONDS:
            break
        sleep(HISTORY_POLL_SECONDS)
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            candidate = _fetch_endpoint(
                snapshot["base_url"], "history", query, min(timeout, remaining)
            )
        except MONITOR_ERRORS:
            continue
        if candidate["series"] and _valid_timestamps(candidate["series"]):
            history = candidate
            results = _energy_by_source(history, lower, upper)
    snapshot["history"] = history
    return history["series"], _preferred_energy(results), results


def _summarize_samples(series, energy_by_source, duration):
    values = {}
    for sample in series:
        for field, value in sample.items():
            if field != "t" and _finite_number(value):
                values.setdefault(field, []).append(value)
    return {
        "duration_s": duration,
        "sample_count": len(series),
        "fields": {
            field: {
                "sample_count": len(samples),
                "min": min(samples),
                "mean": fmean(samples),
                "max": max(samples),
            }
            for field, samples in sorted(values.items())
        },
        "mean_power_w": {
            source: result["wh"] * 3600 / duration
            for source, result in energy_by_source.items()
            if duration > 0 and result["wh"] is not None
        },
    }


def capture_snapshot(base_url, hours, *, record=None, timeout=15):
    query = urlencode({"hours": hours})
    snapshot = {"base_url": base_url, "requested_hours": hours, "errors": {}}
    for endpoint in ("history", "energy"):
        try:
            snapshot[endpoint] = _fetch_endpoint(base_url, endpoint, query, timeout)
        except MONITOR_ERRORS as exc:
            snapshot[endpoint] = None
            snapshot["errors"][endpoint] = type(exc).__name__

    if record is not None:
        start, end = run_interval(record)
        lower, upper = start.timestamp(), end.timestamp()
        series, energy, by_source = _capture_run_energy(snapshot, query, timeout, lower, upper)
        run_series = [sample for sample in series if lower <= sample["t"] <= upper]
        snapshot["run"] = {
            "run_id": record["run_id"],
            "started_at": start.isoformat(),
            "ended_at": end.isoformat(),
            "series": run_series,
            "energy": energy,
            "energy_by_source": by_source,
            "summary": _summarize_samples(run_series, by_source, upper - lower),
        }
    snapshot["captured_at"] = datetime.now(UTC).isoformat()
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
