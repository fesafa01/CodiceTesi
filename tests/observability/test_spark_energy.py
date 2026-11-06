import json
from datetime import UTC, datetime
from io import StringIO
from urllib.parse import urlsplit

import pytest

from codice_tesi.observability import spark, spark_archive


def capture(monkeypatch, *histories, start=2, end=14, step=10):
    requests = []
    elapsed = 0
    snapshots = iter(histories)
    series = None

    def open_monitor(url, *, timeout):
        nonlocal series
        endpoint = urlsplit(url).path
        requests.append(endpoint)
        if endpoint == "/energy":
            payload = {"wh_total": 999}
        else:
            series = next(snapshots, series)
            payload = {"series": series, "step": step}
        return StringIO(json.dumps(payload))

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(spark, "urlopen", open_monitor)
    monkeypatch.setattr(spark, "monotonic", lambda: elapsed)
    monkeypatch.setattr(spark, "sleep", sleep)
    record = {
        "run_id": "synthetic-run",
        "started_at": datetime.fromtimestamp(start, UTC).isoformat(),
        "ended_at": datetime.fromtimestamp(end, UTC).isoformat(),
    }
    snapshot = spark.capture_snapshot("http://monitor.invalid", 0.25, record=record)
    return snapshot, requests, elapsed


def test_interpolates_both_edges_with_irregular_spacing(monkeypatch):
    snapshot, requests, _ = capture(
        monkeypatch,
        [{"t": 17, "gpu_power_w": 70}, {"t": 0, "gpu_power_w": 0}, {"t": 7, "gpu_power_w": 70}],
    )

    assert requests == ["/history", "/energy"]
    assert snapshot["run"]["series"] == [{"t": 7, "gpu_power_w": 70}]
    assert snapshot["run"]["energy"] == {
        "wh": pytest.approx((45 * 5 + 70 * 7) / 3600),
        "source": "gpu",
        "status": "estimated",
        "reason": None,
        "max_sample_gap_s": 10,
    }


def test_estimates_a_short_run_between_two_samples(monkeypatch):
    snapshot, _, _ = capture(
        monkeypatch, [{"t": 0, "gpu_power_w": 60}, {"t": 10, "gpu_power_w": 60}], end=5
    )
    assert snapshot["run"]["series"] == []
    assert snapshot["run"]["energy"]["wh"] == pytest.approx(60 * 3 / 3600)


@pytest.mark.parametrize("last_plug, source, watts", [(90, "plug", 90), (None, "gpu", 30)])
def test_uses_a_single_source(monkeypatch, last_plug, source, watts):
    snapshot, _, _ = capture(
        monkeypatch,
        [
            {"t": 2, "gpu_power_w": 30, "plug_power_w": 90},
            {"t": 14, "gpu_power_w": 30, "plug_power_w": last_plug},
        ],
    )
    energy = snapshot["run"]["energy"]
    assert energy["source"] == source
    assert energy["wh"] == pytest.approx(watts * 12 / 3600)


@pytest.mark.parametrize("missing_power", [False, True])
def test_retries_history_only_for_a_late_final_sample(monkeypatch, missing_power):
    initial = [{"t": 0, "gpu_power_w": 60}, {"t": 10, "gpu_power_w": 60}]
    if missing_power:
        initial.append({"t": 14, "gpu_power_w": None})
    snapshot, requests, elapsed = capture(
        monkeypatch, initial, [*initial, {"t": 20, "gpu_power_w": 60}]
    )

    assert requests == ["/history", "/energy", "/history"]
    assert 0 < elapsed <= spark.HISTORY_RETRY_SECONDS
    assert snapshot["run"]["energy"]["wh"] == pytest.approx(60 * 12 / 3600)
    assert snapshot["history"]["series"][-1]["t"] == 20


@pytest.mark.parametrize(
    "timestamps, step, reason, max_gap",
    [
        ([3, 14], 10, "missing_start_sample", None),
        ([0, 10], 10, "missing_end_sample", None),
        ([0, 30], 10, "sample_gap_too_large", 30),
        ([2, 5, *range(6, 15)], 1, "sample_gap_too_large", 3),
    ],
)
def test_returns_no_wh_when_the_series_cannot_cover_the_run(
    monkeypatch, timestamps, step, reason, max_gap
):
    snapshot, requests, elapsed = capture(
        monkeypatch, [{"t": t, "gpu_power_w": 60} for t in timestamps], step=step
    )
    assert snapshot["run"]["energy"] == {
        "wh": None,
        "source": "gpu",
        "status": "unavailable",
        "reason": reason,
        "max_sample_gap_s": max_gap,
    }
    assert elapsed <= spark.HISTORY_RETRY_SECONDS
    assert requests.count("/energy") == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_archive_keeps_run_energy_separate_from_monitor_window(monkeypatch, tmp_path, legacy):
    snapshot, _, _ = capture(
        monkeypatch, [{"t": 2, "gpu_power_w": 60}, {"t": 14, "gpu_power_w": 60}]
    )
    expected = snapshot["run"]["energy"]
    if legacy:
        del snapshot["run"]["energy"]
        expected = None
    spark.save_snapshot(tmp_path / "spark.json", snapshot)
    draft = tmp_path / "synthetic" / "bozza.txt"
    draft.parent.mkdir()
    draft.write_text("Fixture sintetica", encoding="utf-8")
    (draft.parent / "evidenze.md").write_text("Fonte sintetica", encoding="utf-8")
    monkeypatch.setattr(
        spark_archive,
        "urlopen",
        lambda *args, **kwargs: StringIO('{"note_id":"fixture","text":"Originale sintetico"}'),
    )
    record = {
        **{key: snapshot["run"][key] for key in ("run_id", "started_at", "ended_at")},
        "test_spark": {
            "archive_version": spark_archive.ARCHIVE_VERSION,
            "artifact_directory": "synthetic",
            "context_id": "fixture",
            "dataset_version": "1",
        },
        "status": "completed",
        "model_config": {"model": "fixture", "thinking": False, "tools_enabled": False},
        "spark_monitor": {"status": "captured", "path": "spark.json"},
        "error_type": None,
        "llm": {},
        "timings": {},
        "trace_id": None,
        "telemetry": {},
    }
    destination = tmp_path / "archive"
    spark_archive.archive_run(
        destination, tmp_path / "registro/run.json", record, base_url="http://test", token="fixture"
    )

    hardware = json.loads((destination / "metriche.json").read_text())["hardware_spark"]
    assert hardware["energy_in_run"] == expected
    assert hardware["energy_monitor_window"] == {"wh_total": 999}
