"""Controlli contro doppio conteggio, imputazioni e integrazioni fuori intervallo."""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from campaign_metrics import (  # noqa: E402
    DataError,
    TechnicalReader,
    collect,
    extract_usage,
    hardware_metrics,
    integrate_power,
    paired_differences,
    select_attempts,
    summarize,
)


def test_energy_clips_boundaries_and_never_uses_whole_monitor_window():
    samples = [
        {"t": 0, "gpu_power_w": 0},
        {"t": 10, "gpu_power_w": 100},
        {"t": 20, "gpu_power_w": 0},
        {"t": 30, "gpu_power_w": 10000},
    ]
    # Da 5 a 15 s, due trapezi: 375 + 375 joule.
    result = integrate_power(samples, 5, 15)
    assert result["wh"] == pytest.approx(750 / 3600)
    assert result["max_gap_seconds"] == 10


@pytest.mark.parametrize("start,end", [(-1, 10), (5, 21)])
def test_energy_does_not_extrapolate(start, end):
    samples = [{"t": t, "gpu_power_w": 80} for t in (0, 10, 20)]
    assert integrate_power(samples, start, end)["wh"] is None


def test_energy_rejects_gaps_and_conflicting_samples():
    samples = [{"t": 0, "gpu_power_w": 80}, {"t": 30, "gpu_power_w": 80}]
    assert integrate_power(samples, 1, 29)["reason"] == "sample_gap_too_large"
    samples = [{"t": 0, "gpu_power_w": 80}, {"t": 0, "gpu_power_w": 90}]
    with pytest.raises(DataError, match="discordanti"):
        integrate_power(samples, 1, 9)


def test_zero_power_is_measured_zero_and_missing_plug_stays_missing():
    samples = [{"t": t, "gpu_power_w": 0} for t in (0, 10, 20)]
    metrics, _ = hardware_metrics(samples, 1, 19, 10)
    assert metrics["gpu_energy_wh"] == 0
    assert metrics["gpu_mean_power_w"] == 0
    assert metrics["plug_energy_wh"] is None


def test_hardware_peak_excludes_samples_outside_run():
    samples = [
        {"t": t, "gpu_power_w": watts, "mem_used_bytes": 2**31}
        for t, watts in ((0, 1000), (10, 50), (20, 60), (30, 1000))
    ]
    metrics, points = hardware_metrics(samples, 5, 25, 10)
    assert metrics["gpu_power_w_sample_max"] == 60
    assert metrics["system_memory_peak_gib"] == 2
    assert len(points) == 2


def usage_fixture():
    return {
        "usage_complete": True,
        "usage_issues": [],
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cached_input_tokens": 10,
        "cycle_duration_seconds": 10,
        "requests": [
            {"input_tokens": 40, "output_tokens": 5, "total_tokens": 45, "cached_input_tokens": 0},
            {
                "input_tokens": 60,
                "output_tokens": 15,
                "total_tokens": 75,
                "cached_input_tokens": 10,
            },
        ],
    }


def test_token_aggregation_counts_requests_once():
    result = extract_usage(usage_fixture())
    assert result["input_tokens"] == 100
    assert result["total_tokens"] == 120
    assert result["output_tokens_per_cycle_second"] == 2


def test_token_request_sum_must_match_aggregate():
    usage = usage_fixture()
    usage["requests"][0]["input_tokens"] += 1
    with pytest.raises(DataError, match="Somma"):
        extract_usage(usage)


@pytest.mark.parametrize(
    "field,value", [("total_tokens", 121), ("input_tokens", 101), ("cached_input_tokens", 200)]
)
def test_token_inconsistency_stops_analysis(field, value):
    usage = usage_fixture()
    usage[field] = value
    with pytest.raises(DataError):
        extract_usage(usage)


def test_incomplete_usage_does_not_turn_partial_counts_into_full_measurements():
    usage = usage_fixture()
    usage["usage_complete"] = False
    result = extract_usage(usage)
    assert result["input_tokens"] is None
    assert result["output_tokens_per_cycle_second"] is None
    assert result["cycle_seconds"] == 10


def test_attempt_policy_depends_on_time_not_success():
    base = {"campaign": "A", "model": "M", "case": "H01", "reasoning": "acceso"}
    early = {**base, "started_at": "2026-11-07T10:00:00+00:00", "status": "completed"}
    late = {**base, "started_at": "2026-11-07T11:00:00+00:00", "status": "failed"}
    assert select_attempts([late, early], "latest") == [late]
    assert late["selected"] and not early["selected"]
    assert select_attempts([late, early], "first") == [early]


def test_pairs_preserve_failed_and_do_not_pair_different_cases():
    rows = [
        {
            "model": "M",
            "case": "H01",
            "reasoning": mode,
            "run_id": mode,
            "status": status,
            "gpu_energy_wh": energy,
        }
        for mode, status, energy in (("spento", "completed", 2), ("acceso", "failed", 3))
    ]
    rows.append({**rows[0], "case": "H02"})
    result = paired_differences(rows, "intra")
    assert len(result) == 1
    assert result[0]["gpu_energy_wh_difference"] == 1
    assert result[0]["gpu_energy_wh_ratio"] == 1.5
    assert result[0]["to_status"] == "failed"


def test_missing_values_have_separate_denominators():
    rows = [
        {"model": "M", "reasoning": "spento", "status": "completed", "gpu_energy_wh": 2},
        {"model": "M", "reasoning": "spento", "status": "failed", "gpu_energy_wh": None},
    ]
    result = summarize(rows)[0]
    assert result["n"] == 2 and result["failed"] == 1
    assert result["metrics"]["gpu_energy_wh"]["n"] == 1
    assert result["metrics"]["gpu_energy_wh"]["mean"] == 2


def test_reader_refuses_clinical_files_and_source_mutation(tmp_path):
    reader = TechnicalReader(tmp_path)
    with pytest.raises(DataError, match="whitelist"):
        reader.read(tmp_path / "bozza.txt")
    path = tmp_path / "scheda.json"
    path.write_text('{"status":"completed"}')
    reader.read(path)
    path.write_text('{"status":"failed"}')
    with pytest.raises(DataError, match="modificata"):
        reader.read(path)


def write_case(root, key, enabled, *, failed=False, snapshot=True, extra=""):
    directory = root / f"{key}-{'on' if enabled else 'off'}{extra}"
    directory.mkdir(parents=True)
    display = f"Caso Hosp{' e ED' if key.startswith('HE') else ''} {int(key.lstrip('HE'))}"
    run_id = directory.name
    card = {
        "run_id": run_id,
        "display_name": display,
        "context_id": key,
        "dataset_version": "same-version",
        "model": "qwen3.8:27b",
        "status": "failed" if failed else "completed",
        "reasoning_enabled": enabled,
        "tools_enabled": enabled,
        "started_at": "2026-11-07T00:00:01+00:00",
        "ended_at": "2026-11-07T00:00:19+00:00",
    }
    (directory / "scheda.json").write_text(json.dumps(card))
    (directory / "metriche.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": card["status"],
                "token_e_stream": usage_fixture(),
            }
        )
    )
    if snapshot:
        start = datetime.fromisoformat(card["started_at"]).timestamp() - 1
        (directory / "spark.json").write_text(
            json.dumps(
                {
                    "run": {key: card[key] for key in ("run_id", "started_at", "ended_at")},
                    "history": {
                        "step": 10,
                        "series": [
                            {"t": start + offset, "gpu_power_w": 80} for offset in (0, 10, 20)
                        ],
                    },
                }
            )
        )
    # Un contenuto intenzionalmente illeggibile: l'estrattore deve ignorarlo.
    (directory / "bozza.txt").write_bytes(b"\xff\x00DO_NOT_READ")
    return directory


def test_collect_excludes_incomplete_campaign_recovers_hardware_and_ignores_drafts(tmp_path):
    complete = tmp_path / "TEST-SPARK-QWEN"
    for pathway in ("H", "HE"):
        for number in range(1, 16):
            for enabled in (False, True):
                key = f"{pathway}{number:02}"
                write_case(
                    complete,
                    key,
                    enabled,
                    failed=(key == "HE15" and enabled),
                    snapshot=(key != "H01" or enabled),
                )
    incomplete = tmp_path / "TEST-SPARK-GEMMA"
    write_case(incomplete, "H01", False)
    # Questa metrica corrotta non deve essere letta nella campagna incompleta.
    (incomplete / "H01-off/metriche.json").write_text("not json")
    (complete / "H01-on/risultati_tool.json").write_text(
        json.dumps({"run_id": "H01-on", "results": [{"tool": "A"}, {"tool": "B"}]})
    )
    result = collect(tmp_path)
    assert len(result["selected"]) == 60
    assert len(result["excluded"]) == 1
    assert result["excluded"][0]["campaign"] == "TEST-SPARK-GEMMA"
    assert sum(row["status"] == "failed" for row in result["selected"]) == 1
    recovered = [row for row in result["selected"] if row["hardware_recovered"]]
    assert len(recovered) == 1
    assert recovered[0]["gpu_energy_wh"] == pytest.approx(80 * 18 / 3600)
    assert sum(row["saved_tool_results"] is None for row in result["selected"]) == 59
    assert (
        next(row for row in result["selected"] if row["run_id"] == "H01-on")["saved_tool_results"]
        == 2
    )
    assert all(
        source["path"].endswith(
            ("scheda.json", "metriche.json", "spark.json", "risultati_tool.json")
        )
        for source in result["sources"]
    )
