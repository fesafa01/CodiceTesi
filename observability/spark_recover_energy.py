"""Ricalcola i Wh delle prove concluse dagli snapshot tecnici Spark."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from codice_tesi.observability.spark import estimate_run_energy, save_snapshot  # noqa: E402


def recover(snapshots):
    results = []
    for snapshot in snapshots:
        run = snapshot["run"]
        run_id = run["run_id"]
        energy = run.get("energy")
        if (
            isinstance(energy, dict)
            and energy.get("status") == "estimated"
            and energy.get("wh") is not None
        ):
            results.append({"run_id": run_id, "energy_in_run": energy, "status": "already_saved"})
            continue

        energy = estimate_run_energy(run, snapshot.get("history"))
        source_id = run_id
        if energy["status"] != "estimated":
            for other in snapshots:
                if other is snapshot:
                    continue
                candidate = estimate_run_energy(run, other.get("history"))
                if candidate["status"] == "estimated":
                    energy = candidate
                    source_id = other["run"]["run_id"]
                    break

        result = {"run_id": run_id, "energy_in_run": energy, "status": "unavailable"}
        if energy["status"] == "estimated":
            result.update(status="recovered", history_run_id=source_id)
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshots", nargs="+", type=Path, help="Snapshot tecnici Spark in JSON")
    parser.add_argument("--output", required=True, type=Path, help="Nuovo file JSON di riepilogo")
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("Indica un nuovo file di riepilogo.")

    try:
        snapshots = [json.loads(path.read_text(encoding="utf-8")) for path in args.snapshots]
        results = recover(snapshots)
        save_snapshot(args.output, {"runs": results})
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        parser.exit(1, f"Recupero Spark fallito ({type(error).__name__}).\n")

    recovered = sum(result["status"] == "recovered" for result in results)
    unavailable = sum(result["status"] == "unavailable" for result in results)
    print(f"{recovered} stime recuperate, {unavailable} indisponibili su {len(results)} run.")
    print(args.output)


if __name__ == "__main__":
    main()
