"""Estrazione quantitativa dai soli archivi tecnici delle campagne Spark.

Non apre testi clinici, bozze, evidenze, registri applicativi o checkpoint.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import fmean, median

MODES = ("spento", "acceso")
CONDITIONS = {(False, False), (True, True)}
EXPECTED_CASES = {f"{path}{number:02}" for path in ("H", "HE") for number in range(1, 16)}
ALLOWED_FILES = {"scheda.json", "metriche.json", "spark.json", "risultati_tool.json"}
MODEL_NAMES = {
    "frob/mistral-small-4:119b": "Mistral Small 4",
    "qwen3.8:27b": "Qwen 3.8",
    "gemma4:31b": "Gemma 4",
}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens")
HARDWARE_FIELDS = ("gpu_power_w", "gpu_util_pct", "gpu_temp_c", "mem_used_bytes", "load1")


class DataError(ValueError):
    """Dati tecnici incoerenti: interrompere l'analisi senza correggerli in silenzio."""


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def timestamp(value):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise DataError("Timestamp senza fuso orario")
    return date.timestamp()


def case_key(display_name):
    match = re.fullmatch(r"Caso Hosp( e ED)? (\d+)", display_name)
    if not match:
        raise DataError(f"Nome caso non riconosciuto: {display_name!r}")
    key = f"{'HE' if match[1] else 'H'}{int(match[2]):02}"
    if key not in EXPECTED_CASES:
        raise DataError(f"Caso fuori campione: {key}")
    return key


class TechnicalReader:
    """Whitelist di file e hash delle sole fonti effettivamente lette."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.sources = {}

    def read(self, path):
        path = Path(path)
        if path.name not in ALLOWED_FILES:
            raise DataError(f"File fuori whitelist: {path.name}")
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or path.is_symlink():
            raise DataError("Fonte esterna all'archivio o collegamento simbolico")
        raw = path.read_bytes()
        relative = resolved.relative_to(self.root).as_posix()
        digest = hashlib.sha256(raw).hexdigest()
        if relative in self.sources and self.sources[relative]["sha256"] != digest:
            raise DataError(f"Fonte modificata durante l'analisi: {relative}")
        self.sources[relative] = {"path": relative, "sha256": digest, "bytes": len(raw)}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise DataError(f"Atteso oggetto JSON: {relative}")
        return data


def extract_usage(usage):
    """Usa contatori provider completi; non converte stime di byte in token."""
    usage = usage or {}
    result = {field: None for field in TOKEN_FIELDS}
    complete = usage.get("usage_complete") is True and not usage.get("usage_issues")
    if complete:
        for field in TOKEN_FIELDS:
            value = usage.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise DataError(f"Contatore token non valido: {field}")
            result[field] = value
        inp, out, total = (result[key] for key in TOKEN_FIELDS[:3])
        if None not in (inp, out, total) and inp + out != total:
            raise DataError("Il totale token non coincide con input + output")
        cached = result["cached_input_tokens"]
        if cached is not None and inp is not None and cached > inp:
            raise DataError("Token in cache maggiori dei token di input")
        for field in TOKEN_FIELDS:
            requests = usage.get("requests") or []
            counts = [request.get(field) for request in requests]
            if counts and all(type(value) is int and value >= 0 for value in counts):
                if result[field] is not None and sum(counts) != result[field]:
                    raise DataError(f"Somma delle richieste incoerente: {field}")
    result["usage_complete"] = bool(complete)
    result["usage_source"] = usage.get("usage_source")
    # Le latenze sono riferite al ciclo, già aggregato dal registratore.
    fields = {
        "cycle_seconds": "cycle_duration_seconds",
        "provider_seconds": "provider_requests_duration_seconds",
        "first_content_seconds": "time_to_first_content_seconds",
        "first_text_seconds": "time_to_first_text_seconds",
        "final_text_seconds": "time_to_final_response_first_text_seconds",
        "model_requests": "model_requests_started",
        "tools_requested": "tool_calls_requested",
    }
    for destination, source in fields.items():
        value = usage.get(source)
        result[destination] = value if finite(value) and value >= 0 else None
    duration = result["cycle_seconds"]
    output = result["output_tokens"]
    result["output_tokens_per_cycle_second"] = (
        output / duration if output is not None and duration and duration > 0 else None
    )
    return result


def integrate_power(series, start, end, field="gpu_power_w", step=10):
    """Trapezi con interpolazione ai bordi, senza estrapolazione o gap >20 s."""
    values = {}
    for sample in series:
        t, watts = sample.get("t"), sample.get(field)
        if finite(t) and finite(watts) and watts >= 0:
            if t in values and values[t] != watts:
                raise DataError("Campioni discordanti allo stesso timestamp")
            values[t] = watts
    times = sorted(values)
    if not times or end <= start:
        return {"wh": None, "reason": "no_power_samples", "max_gap_seconds": None}
    left = bisect_right(times, start) - 1
    right = bisect_left(times, end)
    if left < 0 or right == len(times):
        return {"wh": None, "reason": "missing_boundary", "max_gap_seconds": None}
    intervals = list(zip(times[left:right], times[left + 1 : right + 1], strict=True))
    max_gap = max((b - a for a, b in intervals), default=0)
    limit = min(20, 2 * step) if finite(step) and step > 0 else 20
    if max_gap > limit:
        return {"wh": None, "reason": "sample_gap_too_large", "max_gap_seconds": max_gap}
    joules = 0.0
    for a, b in intervals:
        lower, upper = max(a, start), min(b, end)
        slope = (values[b] - values[a]) / (b - a)
        first = values[a] + slope * (lower - a)
        last = values[a] + slope * (upper - a)
        joules += (first + last) / 2 * (upper - lower)
    return {"wh": joules / 3600, "reason": None, "max_gap_seconds": max_gap}


def hardware_metrics(series, start, end, step):
    rows = [sample for sample in series if finite(sample.get("t")) and start <= sample["t"] <= end]
    energy = integrate_power(series, start, end, step=step)
    plug = integrate_power(series, start, end, field="plug_power_w", step=step)
    result = {
        "gpu_energy_wh": energy["wh"],
        "gpu_energy_method": "trapezoidal_interpolation" if energy["wh"] is not None else None,
        "gpu_energy_missing_reason": energy["reason"],
        "gpu_energy_max_gap_seconds": energy["max_gap_seconds"],
        "plug_energy_wh": plug["wh"],
        "plug_energy_missing_reason": plug["reason"],
        "hardware_samples": len(rows),
        "gpu_mean_power_w": energy["wh"] * 3600 / (end - start)
        if energy["wh"] is not None
        else None,
    }
    for field in HARDWARE_FIELDS:
        values = [
            sample[field] for sample in rows if finite(sample.get(field)) and sample[field] >= 0
        ]
        result[f"{field}_sample_mean"] = fmean(values) if values else None
        result[f"{field}_sample_max"] = max(values) if values else None
    memory = result["mem_used_bytes_sample_max"]
    result["system_memory_peak_gib"] = memory / 2**30 if memory is not None else None
    return result, rows


def select_attempts(rows, policy="latest"):
    if policy not in {"first", "latest"}:
        raise ValueError("Politica tentativi sconosciuta")
    groups = defaultdict(list)
    for row in rows:
        groups[(row["campaign"], row["case"], row["reasoning"])].append(row)
    selected = []
    for attempts in groups.values():
        ordered = sorted(attempts, key=lambda row: timestamp(row["started_at"]))
        if len({timestamp(row["started_at"]) for row in ordered}) != len(ordered):
            raise DataError("Tentativi ambigui: stesso istante di avvio")
        chosen = ordered[-1] if policy == "latest" else ordered[0]
        for index, row in enumerate(ordered, 1):
            row["attempt_policy"] = policy
            row["archived_attempt"] = index
            row["archived_attempts_in_cell"] = len(ordered)
            row["selected"] = row is chosen
        selected.append(chosen)
    return sorted(selected, key=lambda row: (row["model"], row["case"], row["reasoning"]))


def collect(private, policy="latest"):
    reader = TechnicalReader(private)
    campaigns, excluded, all_rows, profiles = [], [], [], []
    all_contexts, all_datasets = {}, set()
    for directory in sorted(reader.root.glob("TEST-SPARK-*")):
        if not directory.is_dir():
            continue
        cards = [(path, reader.read(path)) for path in sorted(directory.glob("*/scheda.json"))]
        cells = defaultdict(list)
        nonterminal = 0
        for path, card in cards:
            flags = (card.get("reasoning_enabled"), card.get("tools_enabled"))
            if any(type(flag) is not bool for flag in flags) or flags not in CONDITIONS:
                raise DataError(f"Condizione sperimentale inattesa: {path.parent.name}")
            key = (case_key(card["display_name"]), flags[0])
            cells[key].append((path, card))
            nonterminal += card.get("status") not in {"completed", "failed"}
        expected = {(case, enabled) for case in EXPECTED_CASES for enabled in (False, True)}
        if set(cells) != expected or nonterminal:
            excluded.append(
                {
                    "campaign": directory.name,
                    "reason": "campaign_incomplete",
                    "terminal_cells": sum(
                        any(c["status"] in {"completed", "failed"} for _, c in v)
                        for v in cells.values()
                    ),
                    "expected_cells": len(expected),
                    "nonterminal_archives": nonterminal,
                }
            )
            continue
        models = {card["model"] for _, card in cards}
        if len(models) != 1:
            raise DataError(f"Più modelli nella campagna {directory.name}")
        model_id = models.pop()
        snapshots = []
        for path, card in cards:
            spark_path = path.with_name("spark.json")
            if spark_path.exists():
                snapshot = reader.read(spark_path)
                run = snapshot.get("run") or {}
                for key in ("run_id", "started_at", "ended_at"):
                    actual = run.get(key)
                    equal = (
                        actual == card[key]
                        if key == "run_id"
                        else (actual is not None and timestamp(actual) == timestamp(card[key]))
                    )
                    if not equal:
                        raise DataError(f"Snapshot e scheda discordanti: {path.parent.name}/{key}")
                history = snapshot.get("history") or {}
                snapshots.append((spark_path, history, snapshot))
        by_path = {path: (history, snapshot) for path, history, snapshot in snapshots}
        for path, card in cards:
            case = case_key(card["display_name"])
            identity = (card.get("dataset_version"), card.get("context_id"))
            if case in all_contexts and all_contexts[case] != identity:
                raise DataError(f"Casi non appaiabili tra archivi: {case}")
            all_contexts[case] = identity
            all_datasets.add(card.get("dataset_version"))
            metrics = reader.read(path.with_name("metriche.json"))
            if metrics.get("run_id") != card["run_id"] or metrics.get("status") != card["status"]:
                raise DataError(f"Scheda e metriche discordanti: {path.parent.name}")
            start, end = timestamp(card["started_at"]), timestamp(card["ended_at"])
            if end <= start:
                raise DataError("Durata dell'esecuzione non positiva")
            own = path.with_name("spark.json")
            # Preferisce lo snapshot della prova. Recupera solo da uno storico della stessa
            # campagna che copra l'intero intervallo e rispetti i limiti sui gap.
            candidates = ([own] if own in by_path else []) + [
                p for p, _, _ in snapshots if p != own
            ]
            hardware = None
            source = None
            observed = []
            for candidate in candidates:
                history, snapshot = by_path[candidate]
                values, samples = hardware_metrics(
                    history.get("series") or [], start, end, history.get("step")
                )
                if hardware is None:
                    hardware, observed, source = values, samples, candidate
                if values["gpu_energy_wh"] is not None:
                    hardware, observed, source = values, samples, candidate
                    break
            if hardware is None:
                hardware, observed = hardware_metrics([], start, end, None)
            # Una stima già salvata deve concordare con il ricalcolo degli stessi campioni.
            if source == own:
                _, snap = by_path[own]
                saved = ((snap.get("run") or {}).get("energy_by_source") or {}).get("gpu") or {}
                saved_wh = saved.get("wh")
                calculated_wh = hardware["gpu_energy_wh"]
                if (
                    finite(saved_wh)
                    and calculated_wh is not None
                    and not math.isclose(saved_wh, calculated_wh, rel_tol=1e-9, abs_tol=1e-9)
                ):
                    raise DataError(f"Energia ricalcolata discordante: {path.parent.name}")
            usage = extract_usage(metrics.get("token_e_stream"))
            row = {
                "campaign": directory.name,
                "model": MODEL_NAMES.get(model_id, model_id),
                "model_id": model_id,
                "case": case,
                "pathway": "Hosp + ED" if case.startswith("HE") else "Hosp",
                "reasoning": "acceso" if card["reasoning_enabled"] else "spento",
                "tools": "accesi" if card["tools_enabled"] else "spenti",
                "status": card["status"],
                "run_id": card["run_id"],
                "started_at": card["started_at"],
                "ended_at": card["ended_at"],
                "duration_seconds": end - start,
                "metrics_source": path.with_name("metriche.json")
                .relative_to(reader.root)
                .as_posix(),
                "hardware_source": source.relative_to(reader.root).as_posix() if source else None,
                "hardware_recovered": source is not None and source != own,
                **usage,
                **hardware,
            }
            tool_path = path.with_name("risultati_tool.json")
            row["saved_tool_results"] = None
            if tool_path.exists():
                tool_data = reader.read(tool_path)
                if tool_data.get("run_id") != card["run_id"] or not isinstance(
                    tool_data.get("results"), list
                ):
                    raise DataError(f"Output tool incoerente: {path.parent.name}")
                row["saved_tool_results"] = len(tool_data["results"])
            energy, output = row["gpu_energy_wh"], row["output_tokens"]
            row["gpu_joules_per_output_token"] = (
                energy * 3600 / output if energy is not None and output and output > 0 else None
            )
            all_rows.append(row)
            profiles.extend(
                {
                    "run_id": row["run_id"],
                    "case": case,
                    "model": row["model"],
                    "reasoning": row["reasoning"],
                    "elapsed_seconds": sample["t"] - start,
                    **{field: sample.get(field) for field in HARDWARE_FIELDS},
                }
                for sample in observed
            )
        campaigns.append(
            {
                "campaign": directory.name,
                "model": MODEL_NAMES.get(model_id, model_id),
                "model_id": model_id,
                "archived_runs": len(cards),
                "cells": len(cells),
            }
        )
    if not campaigns:
        raise DataError("Nessuna campagna completa negli archivi")
    if len(all_datasets) != 1 or None in all_datasets:
        raise DataError("Versione del campione assente o diversa tra campagne")
    selected = select_attempts(all_rows, policy)
    if len({row["run_id"] for row in all_rows}) != len(all_rows):
        raise DataError("Identificativo di esecuzione duplicato negli archivi")
    # Due campagne dello stesso modello richiedono un confronto esplicito, senza pooling.
    if len({row["model_id"] for row in campaigns}) != len(campaigns):
        raise DataError("Più campagne dello stesso modello: scegliere esplicitamente l'archivio")
    return {
        "selected": selected,
        "attempts": all_rows,
        "profiles": profiles,
        "campaigns": campaigns,
        "excluded": excluded,
        "sources": sorted(reader.sources.values(), key=lambda source: source["path"]),
        "dataset_version": next(iter(all_datasets)),
        "attempt_policy": policy,
    }


def describe(values):
    values = [value for value in values if finite(value)]
    return {
        "n": len(values),
        "mean": fmean(values) if values else None,
        "median": median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def numeric_fields(rows):
    excluded = {
        "selected",
        "archived_attempt",
        "archived_attempts_in_cell",
        "usage_complete",
        "hardware_recovered",
    }
    return sorted({key for row in rows for key, value in row.items() if finite(value)} - excluded)


def summarize(rows):
    result = []
    groups = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["reasoning"])].append(row)
    for (model, reasoning), group in sorted(groups.items()):
        counts = Counter(row["status"] for row in group)
        result.append(
            {
                "model": model,
                "reasoning": reasoning,
                "n": len(group),
                "completed": counts["completed"],
                "failed": counts["failed"],
                "metrics": {
                    key: describe([row.get(key) for row in group]) for key in numeric_fields(rows)
                },
            }
        )
    return result


def paired_differences(rows, kind):
    """Differenze descrittive su identici casi; non elimina le esecuzioni failed."""
    if kind not in {"intra", "inter"}:
        raise ValueError("Confronto sconosciuto")
    fields, output = numeric_fields(rows), []
    groups = defaultdict(dict)
    for row in rows:
        key = (row["model"], row["case"]) if kind == "intra" else (row["reasoning"], row["case"])
        condition = row["reasoning"] if kind == "intra" else row["model"]
        if condition in groups[key]:
            raise DataError("Cella duplicata nel confronto appaiato")
        groups[key][condition] = row
    conditions = MODES if kind == "intra" else tuple(sorted({row["model"] for row in rows}))
    if len(conditions) != 2:
        return output
    for (group, case), pairs in sorted(groups.items()):
        if not all(condition in pairs for condition in conditions):
            continue
        a, b = (pairs[condition] for condition in conditions)
        result = {
            "group": group,
            "case": case,
            "from": conditions[0],
            "to": conditions[1],
            "from_status": a["status"],
            "to_status": b["status"],
            "from_run_id": a["run_id"],
            "to_run_id": b["run_id"],
        }
        for field in fields:
            av, bv = a.get(field), b.get(field)
            result[f"{field}_difference"] = bv - av if finite(av) and finite(bv) else None
            result[f"{field}_ratio"] = bv / av if finite(av) and finite(bv) and av > 0 else None
        output.append(result)
    return output


def write_json(path, data):
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def write_csv(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
