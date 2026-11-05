"""Salva la telemetria del DGX Spark e i campioni associati a una prova."""

import argparse
import json
import sys
from datetime import datetime
from math import isfinite
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from codice_tesi.observability.spark import (  # noqa: E402
    SPARK_MONITOR_URL,
    capture_snapshot,
    run_interval,
    save_snapshot,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=SPARK_MONITOR_URL, help="Indirizzo HTTP del monitor")
    parser.add_argument("--hours", type=float, help="Finestra in ore (24, oppure 0.25 con --run)")
    parser.add_argument("--run", type=Path, help="File JSON del registro della prova")
    parser.add_argument("--output", type=Path, help="Percorso del JSON da salvare")
    args = parser.parse_args()
    hours = args.hours if args.hours is not None else (0.25 if args.run else 24)
    if not isfinite(hours) or hours <= 0:
        parser.error("--hours deve essere un numero finito maggiore di zero")

    try:
        record = None
        if args.run:
            record = json.loads(args.run.read_text(encoding="utf-8"))
            run_interval(record)
        snapshot = capture_snapshot(args.base_url, hours, record=record)
        timestamp = datetime.fromisoformat(snapshot["captured_at"])
        destination = args.output or (
            ROOT / "output/metriche/spark" / f"{timestamp:%Y%m%dT%H%M%S%fZ}.json"
        )
        save_snapshot(destination, snapshot)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Raccolta Spark fallita: {error}\n")

    print(destination)
    if snapshot["history"] is not None:
        print(f"{len(snapshot['history']['series'])} campioni nella finestra")
    if record and "history" not in snapshot["errors"]:
        print(f"{len(snapshot['run']['series'])} campioni nell'intervallo della prova")
    if snapshot["errors"]:
        endpoints = ", ".join(snapshot["errors"])
        parser.exit(1, f"Raccolta Spark incompleta. Endpoint in errore: {endpoints}\n")


if __name__ == "__main__":
    main()
