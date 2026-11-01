"""Perimetro hardware essenziale, dati mancanti e correlazione delle richieste."""

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from urllib.request import urlopen

import pytest

from codice_tesi.observability import hardware
from codice_tesi.observability.hardware import (
    HardwareConfig,
    HardwareObservation,
    validate_snapshot,
)
from codice_tesi.observability.hardware_probe import (
    METRICS,
    Probe,
    SamplingServer,
    gpu_measurements,
    prometheus_text,
)

GPU_XML = """<nvidia_smi_log><driver_version>580.1</driver_version><gpu>
<product_name>NVIDIA GB10</product_name>
<utilization><gpu_util>50 %</gpu_util><memory_util>12 %</memory_util></utilization>
<fb_memory_usage><total>N/A</total><used>Not Supported</used></fb_memory_usage>
<gpu_power_readings><power_draw>42.50 W</power_draw></gpu_power_readings>
<temperature><gpu_temp>55</gpu_temp></temperature>
<clocks><graphics_clock>1200 MHz</graphics_clock><mem_clock>N/A</mem_clock></clocks>
</gpu></nvidia_smi_log>"""


def snapshot(*, sequence=1, timestamp=None):
    return {
        "schema_version": "1.0",
        "boot_id": "a" * 32,
        "sequence": sequence,
        "sampled_at": (timestamp or datetime.now(UTC)).isoformat(),
        "host": "fixture-spark",
        "sources": {s: {"available": True} for s in ("cpu", "gpu", "memory", "disk")},
        "metrics": [{"name": "spark_gpu_temperature_celsius", "labels": {"gpu": "0"}, "value": 55}],
    }


def test_gpu_exports_only_utilization_temperature_and_graphics_clock():
    metrics, devices, driver = gpu_measurements(GPU_XML)
    assert {m["name"]: m["value"] for m in metrics} == {
        "spark_gpu_utilization_ratio": 0.5,
        "spark_gpu_temperature_celsius": 55,
        "spark_gpu_graphics_clock_hertz": 1_200_000_000,
    }
    assert devices == [{"gpu": "0", "model": "NVIDIA GB10"}] and driver == "580.1"
    # Anche valori numerici di vecchie sonde restano fuori dal nuovo perimetro.
    extra = GPU_XML.replace("N/A", "123").replace("Not Supported", "456")
    assert gpu_measurements(extra)[0] == metrics
    assert {name for name in METRICS if name.startswith("spark_gpu_")} == {
        "spark_gpu_utilization_ratio",
        "spark_gpu_temperature_celsius",
        "spark_gpu_graphics_clock_hertz",
    }


def test_gpu_missing_values_are_omitted_and_real_zero_is_preserved():
    xml = GPU_XML.replace("50 %", "0 %").replace("55", "N/A").replace("1200 MHz", "Not Supported")
    assert gpu_measurements(xml)[0] == [
        {"name": "spark_gpu_utilization_ratio", "labels": {"gpu": "0"}, "value": 0}
    ]


def test_linux_probe_cpu_deltas_and_missing_nvidia_do_not_hide_host_metrics(tmp_path):
    (tmp_path / "stat").write_text("cpu 10 0 10 80 0 0 0 0 999 999\ncpu0 0\n")
    (tmp_path / "loadavg").write_text("0.20 0.30 0.40 1/10 100")
    (tmp_path / "meminfo").write_text(
        "MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 200 kB\nSwapFree: 50 kB\n"
    )

    def unavailable():
        raise FileNotFoundError("PRIVATE_ERROR")

    probe = Probe(proc=tmp_path, disk=tmp_path, gpu_query=unavailable)
    first = probe.sample()
    assert "spark_cpu_utilization_ratio" not in {m["name"] for m in first["metrics"]}
    (tmp_path / "stat").write_text("cpu 20 0 20 100 0 0 0 0 999 999\ncpu0 0\n")
    second = probe.sample()
    values = {m["name"]: m["value"] for m in second["metrics"]}
    assert values["spark_cpu_utilization_ratio"] == 0.5
    assert values["spark_memory_used_bytes"] == 600 * 1024
    assert values["spark_swap_used_bytes"] == 150 * 1024
    assert second["sources"]["gpu"] == {"available": False, "error_type": "FileNotFoundError"}
    assert "PRIVATE_ERROR" not in json.dumps(second)
    assert "spark_gpu_power_watts" not in prometheus_text(second)


@pytest.mark.parametrize("age", [11, -6])
def test_old_or_future_samples_are_rejected(age):
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        validate_snapshot(snapshot(timestamp=now - timedelta(seconds=age)), max_age=10, now=now)


def test_snapshot_allowlist_rejects_nonfinite_and_duplicate_values():
    data = snapshot()
    data["metrics"].extend(
        [
            {"name": "spark_gpu_power_watts", "labels": {"gpu": "0"}, "value": 42.5},
            {"name": "spark_gpu_memory_used_bytes", "labels": {"gpu": "0"}, "value": 1024},
        ]
    )
    assert len(validate_snapshot(data, max_age=10)["metrics"]) == 1
    data["clinical_text"] = "PRIVATE_SOURCE"
    data["metrics"].append({"name": "unknown_metric", "value": "PRIVATE_SOURCE"})
    assert "PRIVATE_SOURCE" not in json.dumps(validate_snapshot(data, max_age=10))
    data["metrics"][0]["value"] = float("nan")
    with pytest.raises(ValueError):
        validate_snapshot(data, max_age=10)
    data = snapshot()
    data["metrics"].append(dict(data["metrics"][0]))
    with pytest.raises(ValueError):
        validate_snapshot(data, max_age=10)


def test_duplicate_and_pre_request_samples_do_not_bias_statistics(tmp_path):
    config = HardwareConfig(enabled=True, _env_file=None)
    observation = HardwareObservation(config, tmp_path, "run-fixture")
    now = datetime.now(UTC)
    observation.started = 1
    observation.started_at = now.isoformat()
    observation._observe(snapshot(timestamp=now - timedelta(seconds=1)))
    first = snapshot(timestamp=now)
    observation._observe(first)
    observation._observe(first)
    second = snapshot(sequence=2, timestamp=now + timedelta(seconds=1))
    second["metrics"][0]["value"] = 45
    observation._observe(second)
    summary = observation.summary()
    assert summary["samples"] == 2
    assert summary["duplicate_samples"] == summary["outside_window_samples"] == 1
    assert summary["statistics"][0]["mean"] == 50
    assert summary["attribution"] == "shared_host_not_exclusive_to_request"
    assert "cost_estimate" not in summary
    changed = snapshot(sequence=3)
    changed["host"] = "another-host"
    with pytest.raises(ValueError):
        observation._observe(changed)
    asyncio.run(observation.stop())
    assert len(observation.path.read_text().splitlines()) == 2


def test_unreachable_probe_is_explicit_and_keeps_exception_text_private(tmp_path, monkeypatch):
    def fail(config):
        raise OSError("PRIVATE_SERVER_ERROR")

    monkeypatch.setattr(hardware, "fetch_snapshot", fail)

    async def run():
        observation = HardwareObservation(
            HardwareConfig(enabled=True, _env_file=None), tmp_path, "run-fixture"
        )
        observation.start()
        await asyncio.sleep(0.03)
        await observation.stop()
        return observation

    observation = asyncio.run(run())
    summary = observation.summary()
    assert summary["status"] == "unavailable" and summary["errors"] == 1
    assert summary["statistics"] == [] and summary["raw_samples_path"] is None
    assert "PRIVATE_SERVER_ERROR" not in json.dumps(summary)
    assert not observation.path.exists()


def test_standalone_exporter_serves_json_and_prometheus_on_same_samples(tmp_path):
    probe = Probe(proc=tmp_path, disk=tmp_path, gpu_query=lambda: GPU_XML)
    with SamplingServer(("127.0.0.1", 0), probe, 1) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}"
            data = hardware.fetch_snapshot(
                HardwareConfig(endpoint=endpoint, enabled=True, _env_file=None)
            )
            clean = validate_snapshot(data, max_age=10)
            assert clean["devices"] == [{"gpu": "0", "model": "NVIDIA GB10"}]
            with urlopen(endpoint + "/metrics", timeout=2) as response:
                text = response.read().decode()
            assert 'spark_gpu_temperature_celsius{gpu="0"} 55.0' in text
            assert 'spark_hardware_source_available{source="cpu"} 0' in text
            assert "spark_gpu_memory_used_bytes" not in text
        finally:
            server.shutdown()
            thread.join(timeout=2)
