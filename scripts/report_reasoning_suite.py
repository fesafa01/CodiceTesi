"""Riepilogo senza contenuti: apre solo scheda.json e metriche.json degli archivi.

Non legge campioni, checkpoint, lettere, evidenze o risultati_tool.json.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

MODELS = {"qwen3.8:27b", "gemma4:31b", "frob/mistral-small-4:119b"}
COUNTERS = ("requested", "completed", "failed", "arguments_rejected", "empty", "nonempty")


def summarize_archives(roots):
    groups = defaultdict(
        lambda: {
            "tests": 0,
            "completed": 0,
            "failed": 0,
            "interrupted": 0,
            "technical_passed": 0,
            "technical_failed": 0,
            "tests_with_tool_requests": 0,
            "tool_metrics_unavailable": 0,
            "tests_with_empty_inventory": 0,
            **{f"tools_{key}": 0 for key in COUNTERS},
        }
    )
    seen = set()
    for root in roots:
        root = Path(root)
        cards = sorted(root.glob("*/scheda.json"))
        if not cards:
            raise ValueError("Archivio senza schede tecniche.")
        for path in cards:
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("Collegamenti simbolici non ammessi.")
            metric_path = path.with_name("metriche.json")
            if metric_path.is_symlink():
                raise ValueError("Collegamenti simbolici non ammessi.")
            card = json.loads(path.read_text())
            metrics = json.loads(metric_path.read_text())
            model = card.get("model")
            if model not in MODELS or card.get("reasoning_enabled") is not True:
                raise ValueError("L'archivio non appartiene alla nuova suite di ragionamento.")
            tools = card.get("tools_enabled")
            if type(tools) is not bool:
                raise ValueError("Modalità non valida.")
            identity = (model, tools, card["context_id"])
            if identity in seen:
                raise ValueError("Più tentativi per lo stesso caso: usare archivi separati.")
            seen.add(identity)
            if card["run_id"] != metrics["run_id"]:
                raise ValueError("Scheda e metriche non appartengono allo stesso tentativo.")
            status = card.get("status")
            if status not in {"completed", "failed", "interrupted"}:
                raise ValueError("Tentativo non concluso nell'archivio.")
            row = groups[model, "reasoning-tools" if tools else "reasoning"]
            row["tests"] += 1
            row[status] += 1
            validation = card.get("draft_technical_validation")
            if validation in {"passed", "failed"}:
                row[f"technical_{validation}"] += 1
            usage = metrics.get("token_e_stream") or {}
            summary = usage.get("tool_summary")
            if tools:
                if summary is None:
                    if status == "completed":
                        raise ValueError(
                            "Metriche dei tool mancanti: possibile archivio precedente."
                        )
                    row["tool_metrics_unavailable"] += 1
                    continue
                if summary.get("extraction_version") != "mimic-record-v1":
                    raise ValueError("Versione di estrazione non prevista.")
                for key in (*COUNTERS, "recognized_facts"):
                    if type(summary.get(key)) is not int or summary[key] < 0:
                        raise ValueError("Contatori tecnici non validi.")
                for key in COUNTERS:
                    row[f"tools_{key}"] += summary[key]
                row["tests_with_tool_requests"] += bool(summary["requested"])
                row["tests_with_empty_inventory"] += summary["recognized_facts"] == 0
    return [
        {"model": model, "condition": condition, **counts}
        for (model, condition), counts in sorted(groups.items())
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        rows = summarize_archives(args.archives)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(2, "Archivi tecnici assenti o incoerenti; nessun contenuto stampato.\n")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
