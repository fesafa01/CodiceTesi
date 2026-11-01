"""Sonda Linux autonoma per lo Spark: sola lettura, solo libreria standard.

Questo file può essere copiato sullo Spark senza installare il progetto. Non
legge richieste LLM, nomi di processi, credenziali o contenuti clinici.
"""

import argparse
import json
import math
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

# Perimetro essenziale Spark: niente framebuffer dedicato o letture energetiche.
# Nome, descrizione e unità restano comuni a exporter e validatore client.
METRICS = {
    "spark_cpu_utilization_ratio": ("Host CPU busy fraction, excluding idle and iowait", "1"),
    "spark_cpu_count": ("Host logical CPU count", "{cpu}"),
    "spark_load1": ("Host one minute load average", "1"),
    "spark_memory_total_bytes": ("Host unified memory total", "By"),
    "spark_memory_available_bytes": ("Host MemAvailable, including reclaimable memory", "By"),
    "spark_memory_used_bytes": ("Host MemTotal minus MemAvailable", "By"),
    "spark_swap_used_bytes": ("Host swap used", "By"),
    "spark_disk_total_bytes": ("Root filesystem capacity", "By"),
    "spark_disk_free_bytes": ("Root filesystem free space", "By"),
    "spark_gpu_utilization_ratio": ("GPU utilization fraction reported by NVIDIA", "1"),
    "spark_gpu_temperature_celsius": ("GPU temperature", "Cel"),
    "spark_gpu_graphics_clock_hertz": ("GPU graphics clock", "Hz"),
}


def number(text):
    """N/A, Not Supported e valori non finiti non diventano zeri."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:[A-Za-z%]+)?\s*", text or "")
    if not match:
        return None
    value = float(match[1])
    return value if math.isfinite(value) else None


def gpu_measurements(xml):
    root = ET.fromstring(xml)
    measurements, devices = [], []
    fields = {
        "spark_gpu_utilization_ratio": ("utilization/gpu_util", 0.01),
        "spark_gpu_temperature_celsius": ("temperature/gpu_temp", 1),
        "spark_gpu_graphics_clock_hertz": ("clocks/graphics_clock", 1_000_000),
    }
    for index, gpu in enumerate(root.findall("gpu")):
        labels = {"gpu": str(index)}
        devices.append({"gpu": str(index), "model": gpu.findtext("product_name")})
        for name, (path, scale) in fields.items():
            value = number(gpu.findtext(path))
            if value is not None:
                measurements.append({"name": name, "labels": labels, "value": value * scale})
    return measurements, devices, root.findtext("driver_version")


class Probe:
    def __init__(self, *, proc=Path("/proc"), disk=Path("/"), gpu_query=None):
        self.proc, self.disk = proc, disk
        self.gpu_query = gpu_query or self._gpu_query
        self.previous_cpu = None
        self.boot_id = uuid4().hex
        self.sequence = 0

    @staticmethod
    def _gpu_query():
        return subprocess.run(
            ["nvidia-smi", "-q", "-x"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout

    def sample(self):
        metrics, sources = [], {}

        def add(name, value):
            if value is not None and math.isfinite(value) and value >= 0:
                metrics.append({"name": name, "labels": {}, "value": value})

        try:
            stat = (self.proc / "stat").read_text().splitlines()
            cpu = [int(v) for v in stat[0].split()[1:9]]
            total, idle = sum(cpu), cpu[3] + cpu[4]
            if self.previous_cpu is not None:
                delta = total - self.previous_cpu[0]
                idle_delta = idle - self.previous_cpu[1]
                if delta > 0 and 0 <= idle_delta <= delta:
                    add("spark_cpu_utilization_ratio", (delta - idle_delta) / delta)
            self.previous_cpu = (total, idle)
            add("spark_cpu_count", sum(bool(re.match(r"cpu\d+ ", line)) for line in stat))
            add("spark_load1", float((self.proc / "loadavg").read_text().split()[0]))
            sources["cpu"] = {"available": True}
        except (OSError, ValueError, IndexError) as error:
            sources["cpu"] = {"available": False, "error_type": type(error).__name__}
        try:
            memory = {}
            for line in (self.proc / "meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                memory[key] = int(value.split()[0]) * 1024
            add("spark_memory_total_bytes", memory["MemTotal"])
            if "MemAvailable" in memory:
                add("spark_memory_available_bytes", memory["MemAvailable"])
                add("spark_memory_used_bytes", memory["MemTotal"] - memory["MemAvailable"])
            if "SwapTotal" in memory and "SwapFree" in memory:
                add("spark_swap_used_bytes", memory["SwapTotal"] - memory["SwapFree"])
            sources["memory"] = {"available": True}
        except (OSError, ValueError, KeyError, IndexError) as error:
            sources["memory"] = {"available": False, "error_type": type(error).__name__}
        try:
            disk = shutil.disk_usage(self.disk)
            add("spark_disk_total_bytes", disk.total)
            add("spark_disk_free_bytes", disk.free)
            sources["disk"] = {"available": True}
        except OSError as error:
            sources["disk"] = {"available": False, "error_type": type(error).__name__}
        devices, driver = [], None
        try:
            gpu_metrics, devices, driver = gpu_measurements(self.gpu_query())
            metrics.extend(gpu_metrics)
            sources["gpu"] = {"available": bool(gpu_metrics)}
        except (OSError, subprocess.SubprocessError, ET.ParseError) as error:
            sources["gpu"] = {"available": False, "error_type": type(error).__name__}
        self.sequence += 1
        return {
            "schema_version": "1.0",
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "sampled_at": datetime.now(UTC).isoformat(),
            "host": platform.node(),
            "architecture": platform.machine(),
            "driver_version": driver,
            "devices": devices,
            "sources": sources,
            "metrics": metrics,
        }


def prometheus_text(snapshot):
    lines = []
    for name, (help_text, _) in METRICS.items():
        values = [item for item in snapshot["metrics"] if item["name"] == name]
        if not values:
            continue
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
        for item in values:
            labels = ",".join(f'{key}="{value}"' for key, value in item["labels"].items())
            suffix = "{" + labels + "}" if labels else ""
            lines.append(f"{name}{suffix} {item['value']}")
    lines.extend(
        (
            "# HELP spark_hardware_source_available Last collection succeeded for this source",
            "# TYPE spark_hardware_source_available gauge",
        )
    )
    for source, status in snapshot["sources"].items():
        available = int(status["available"])
        lines.append(f'spark_hardware_source_available{{source="{source}"}} {available}')
    timestamp = datetime.fromisoformat(snapshot["sampled_at"]).timestamp()
    lines.extend(
        (
            "# HELP spark_hardware_sample_timestamp_seconds UTC time of latest completed sample",
            "# TYPE spark_hardware_sample_timestamp_seconds gauge",
            f"spark_hardware_sample_timestamp_seconds {timestamp}",
        )
    )
    return "\n".join(lines) + "\n"


class SamplingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, probe, interval):
        self.stop_sampling = threading.Event()
        self.sampler = None
        super().__init__(address, Handler)
        self.probe, self.interval = probe, interval
        self.snapshot = probe.sample()
        self.sampler = threading.Thread(target=self._sample, daemon=True)
        self.sampler.start()

    def _sample(self):
        while not self.stop_sampling.wait(self.interval):
            self.snapshot = self.probe.sample()

    def server_close(self):
        self.stop_sampling.set()
        if self.sampler is not None:
            self.sampler.join(timeout=3)
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        snapshot = self.server.snapshot
        if self.path == "/snapshot":
            body, content_type = json.dumps(snapshot).encode(), "application/json"
        elif self.path == "/metrics":
            body = prometheus_text(snapshot).encode()
            content_type = "text/plain; version=0.0.4; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Stampa l'inventario dei sensori in JSON."
    )
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9401)
    parser.add_argument("--interval", type=float, default=1)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Eseguire questa sonda Linux sullo Spark, non sul client Mac.")
    if not math.isfinite(args.interval) or args.interval < 0.2:
        parser.error("--interval deve essere finito e almeno 0.2 secondi")
    probe = Probe()
    if args.once:
        probe.sample()
        time.sleep(0.2)
        print(json.dumps(probe.sample(), indent=2))
        return
    with SamplingServer((args.listen, args.port), probe, args.interval) as server:
        print(
            f"Sonda Spark su {args.listen}:{args.port}; campionamento {args.interval}s", flush=True
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
