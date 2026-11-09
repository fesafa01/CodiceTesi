"""Rigenera confronti inter/intramodello dalle campagne Spark complete.

Esempio dalla root del progetto:
    python scripts/genera_grafici_quantitativi.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from textwrap import fill

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-tesi-quantitativi")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/cache-tesi-quantitativi")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from campaign_metrics import (  # noqa: E402
    MODES,
    collect,
    finite,
    paired_differences,
    summarize,
    timestamp,
    write_csv,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
COLORS = {"spento": "#17658a", "acceso": "#ce7023"}
METRICS = {
    "input_tokens": "Token di input per esecuzione",
    "output_tokens": "Token di output per esecuzione",
    "total_tokens": "Token totali per esecuzione",
    "cached_input_tokens": "Token di input in cache",
    "duration_seconds": "Durata totale (s)",
    "cycle_seconds": "Durata del ciclo modello (s)",
    "first_content_seconds": "Primo contenuto del flusso (s)",
    "first_text_seconds": "Primo testo (s)",
    "gpu_energy_wh": "Energia GPU stimata (Wh)",
    "gpu_joules_per_output_token": "Energia GPU stimata / token output (J/token)",
    "gpu_mean_power_w": "Potenza GPU media integrata (W)",
    "gpu_power_w_sample_max": "Picco GPU campionato (W)",
    "gpu_util_pct_sample_mean": "Utilizzo GPU medio campionato (%)",
    "gpu_temp_c_sample_max": "Temperatura GPU massima campionata (°C)",
    "system_memory_peak_gib": "Memoria di sistema massima campionata (GiB)",
    "load1_sample_mean": "Carico di sistema medio (load1)",
    "output_tokens_per_cycle_second": "Token output / durata ciclo (token/s)",
    "model_requests": "Richieste al modello per esecuzione",
    "tools_requested": "Chiamate tool richieste",
    "saved_tool_results": "Risultati tool salvati (solo file presenti)",
}


def configure():
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 11,
            "axes.labelsize": 10,
            "figure.titlesize": 16,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.18,
            "axes.axisbelow": True,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "savefig.facecolor": "white",
        }
    )


def save(fig, out, name, title, note, *, policy=None):
    if policy is not None:
        selection = "Ultima" if policy == "latest" else "Prima"
        note = f"{selection} esecuzione archiviata per caso/modalità. " + note
    note = "\n".join(fill(line, width=145) for line in note.splitlines())
    fig.suptitle(title, y=0.995, fontweight="bold")
    fig.text(0.5, 0.01, note, ha="center", va="bottom", fontsize=8, color="#454545")
    fig.tight_layout(rect=(0.015, 0.075, 0.99, 0.965), h_pad=2.0, w_pad=2.0)
    for suffix in ("png", "svg", "pdf"):
        fig.savefig(out / f"{name}.{suffix}", dpi=190)
    plt.close(fig)


def group_order(rows):
    return [(model, mode) for model in sorted({row["model"] for row in rows}) for mode in MODES]


def group_label(group):
    return f"{group[0]}\nReasoning {group[1]}"


def distributions(rows, fields, out, name, title, *, note=""):
    groups = group_order(rows)
    columns = 2 if len(fields) > 1 else 1
    height = 4.0 * ((len(fields) + columns - 1) // columns)
    fig, axes = plt.subplots(
        (len(fields) + columns - 1) // columns, columns, figsize=(13, height), squeeze=False
    )
    for ax, field in zip(axes.flat, fields, strict=False):
        for x, group in enumerate(groups):
            block = sorted(
                (r for r in rows if (r["model"], r["reasoning"]) == group),
                key=lambda row: row["case"],
            )
            valid = [r for r in block if finite(r.get(field))]
            values = [r[field] for r in valid]
            if values:
                bp = ax.boxplot(
                    [values],
                    positions=[x],
                    widths=0.48,
                    patch_artist=True,
                    showfliers=False,
                    manage_ticks=False,
                    medianprops={"color": "#1d1d1d", "linewidth": 1.7},
                )
                bp["boxes"][0].set(facecolor=COLORS[group[1]], alpha=0.22)
                for row in valid:
                    # Posizione deterministica per caso; nessun rumore nei valori misurati.
                    rank = int(row["case"].lstrip("HE")) + (
                        15 if row["case"].startswith("HE") else 0
                    )
                    jitter = (rank - 15.5) / 100
                    failed = row["status"] == "failed"
                    ax.scatter(
                        x + jitter,
                        row[field],
                        marker="x" if failed else "o",
                        s=45 if failed else 18,
                        linewidth=1.6 if failed else 0,
                        color="#b22d35" if failed else COLORS[group[1]],
                        alpha=0.85,
                    )
            ax.text(
                x,
                1.01,
                f"n={len(valid)}/{len(block)}",
                ha="center",
                fontsize=8,
                transform=ax.get_xaxis_transform(),
            )
        ax.set_xticks(range(len(groups)), [group_label(group) for group in groups], fontsize=8)
        ax.set_title(METRICS[field], pad=24)
        ax.set_ylim(bottom=0)
        ax.ticklabel_format(axis="y", style="plain", useOffset=False)
    for ax in list(axes.flat)[len(fields) :]:
        ax.set_visible(False)
    note = (note + "\n" if note else "") + (
        "Punti: esecuzioni; × rosse: failed; linea: mediana; box: Q1–Q3; baffi: 1,5 IQR. "
        "n = misure disponibili / esecuzioni."
    )
    save(fig, out, name, title, note, policy=rows[0]["attempt_policy"])


def outcomes(rows, out, name="01_esiti", title="Casi completati e failed"):
    groups = group_order(rows)
    fig, ax = plt.subplots(figsize=(12, 5))
    completed, failed = [], []
    for group in groups:
        counts = Counter(r["status"] for r in rows if (r["model"], r["reasoning"]) == group)
        completed.append(counts["completed"])
        failed.append(counts["failed"])
    x = np.arange(len(groups))
    ax.bar(x, completed, color="#287b65", label="Completati", width=0.6)
    ax.bar(x, failed, bottom=completed, color="#ba3c46", label="Failed", width=0.6)
    for i, (a, b) in enumerate(zip(completed, failed, strict=True)):
        if a:
            ax.text(i, a / 2, str(a), ha="center", va="center", color="white", fontweight="bold")
        if b:
            ax.text(
                i, a + b / 2, str(b), ha="center", va="center", color="white", fontweight="bold"
            )
    ax.set_xticks(x, [group_label(group) for group in groups])
    ax.set_ylabel("Numero di esecuzioni")
    ax.set_ylim(0, max(a + b for a, b in zip(completed, failed, strict=True)) * 1.15)
    ax.legend(loc="upper right", ncols=2)
    save(
        fig,
        out,
        name,
        title,
        "Esito operativo riportato nelle schede. Reasoning e strumenti sono commutati insieme.",
        policy=rows[0]["attempt_policy"],
    )


def intra(rows, out):
    models = sorted({row["model"] for row in rows})
    fields = ("duration_seconds", "gpu_energy_wh", "output_tokens", "gpu_mean_power_w")
    fig, axes = plt.subplots(len(fields), len(models), figsize=(13, 13), squeeze=False)
    limits = {
        field: max((r[field] for r in rows if finite(r.get(field))), default=1) * 1.08
        for field in fields
    }
    for column, model in enumerate(models):
        lookup = {(r["case"], r["reasoning"]): r for r in rows if r["model"] == model}
        for ax, field in zip(axes[:, column], fields, strict=True):
            n = 0
            for case in sorted({key[0] for key in lookup}):
                a, b = (lookup.get((case, mode)) for mode in MODES)
                if not a or not b or not finite(a.get(field)) or not finite(b.get(field)):
                    continue
                n += 1
                ax.plot([0, 1], [a[field], b[field]], color="#717f88", alpha=0.3, lw=0.85)
                for x, row in enumerate((a, b)):
                    failed = row["status"] == "failed"
                    ax.scatter(
                        x,
                        row[field],
                        color="#b22d35" if failed else COLORS[MODES[x]],
                        marker="x" if failed else "o",
                        s=40 if failed else 15,
                        zorder=3,
                    )
            ax.set_title(f"{model}\n{fill(METRICS[field], width=43)} · {n} coppie")
            ax.set_xticks([0, 1], ["Reasoning spento", "Reasoning acceso"])
            ax.set_xlim(-0.2, 1.2)
            ax.set_ylim(0, limits[field])
    save(
        fig,
        out,
        "06_confronti_intramodello",
        "Confronti intramodello sugli stessi casi",
        "Una linea per caso; × rosse: failed. Stessa scala per la stessa metrica. "
        "Il confronto comprende anche la commutazione degli strumenti.",
        policy=rows[0]["attempt_policy"],
    )


def inter(rows, out):
    models = sorted({row["model"] for row in rows})
    if len(models) != 2:
        return
    fields = ("duration_seconds", "gpu_energy_wh", "output_tokens", "system_memory_peak_gib")
    fig, axes = plt.subplots(len(fields), 2, figsize=(12, 16), squeeze=False)
    for column, mode in enumerate(MODES):
        lookup = {(r["case"], r["model"]): r for r in rows if r["reasoning"] == mode}
        for ax, field in zip(axes[:, column], fields, strict=True):
            limit = max((r[field] for r in rows if finite(r.get(field))), default=1) * 1.08
            ax.plot([0, limit], [0, limit], ls="--", color="#77848c", lw=0.9)
            n = 0
            for case in sorted({key[0] for key in lookup}):
                a, b = (lookup.get((case, model)) for model in models)
                if not a or not b or not finite(a.get(field)) or not finite(b.get(field)):
                    continue
                failed = "failed" in (a["status"], b["status"])
                n += 1
                ax.scatter(
                    a[field],
                    b[field],
                    color="#b22d35" if failed else COLORS[mode],
                    marker="x" if failed else "o",
                    s=43 if failed else 27,
                    alpha=0.8,
                )
            ax.set_title(f"Reasoning {mode}\n{fill(METRICS[field], width=40)} · {n} coppie")
            ax.set(xlabel=models[0], ylabel=models[1], xlim=(0, limit), ylim=(0, limit))
            ax.set_aspect("equal", adjustable="box")
    save(
        fig,
        out,
        "07_confronti_intermodello",
        "Confronti intermodello sugli stessi casi",
        "Diagonale: valori uguali. × rossa: almeno una esecuzione failed. "
        "Assi e scale identici per la stessa metrica; token contati dai rispettivi provider.",
        policy=rows[0]["attempt_policy"],
    )


def profiles_plot(rows, profiles, out):
    groups = group_order(rows)
    selected = {r["run_id"] for r in rows}
    by_run = defaultdict(list)
    for point in profiles:
        if point["run_id"] in selected:
            by_run[point["run_id"]].append(point)
    fig, axes = plt.subplots(2, len(groups), figsize=(18, 8), squeeze=False)
    for column, group in enumerate(groups):
        block = [r for r in rows if (r["model"], r["reasoning"]) == group]
        for row in block:
            points = sorted(by_run[row["run_id"]], key=lambda p: p["elapsed_seconds"])
            for ax, field in zip(axes[:, column], ("gpu_power_w", "mem_used_bytes"), strict=True):
                good = [p for p in points if finite(p.get(field))]
                factor = 2**30 if field == "mem_used_bytes" else 1
                ax.plot(
                    [p["elapsed_seconds"] for p in good],
                    [p[field] / factor for p in good],
                    color=COLORS[group[1]],
                    alpha=0.23,
                    lw=0.8,
                    marker=".",
                    markersize=1.2,
                )
        axes[0, column].set_title(group_label(group))
        axes[1, column].set_xlabel("Tempo dall'avvio (s)")
    axes[0, 0].set_ylabel("Potenza GPU campionata (W)")
    axes[1, 0].set_ylabel("Memoria di sistema campionata (GiB)")
    xmax = max((p["elapsed_seconds"] for ps in by_run.values() for p in ps), default=1)
    for level in range(2):
        ymax = max(ax.get_ylim()[1] for ax in axes[level])
        for ax in axes[level]:
            ax.set_xlim(0, xmax * 1.02)
            ax.set_ylim(0, ymax)
    save(
        fig,
        out,
        "08_profili_hardware",
        "Profili hardware delle singole esecuzioni",
        "Una linea per esecuzione; punti osservati, tempo reale dall'avvio. "
        "Scale comuni; nessuna normalizzazione temporale né curva sintetica.",
        policy=rows[0]["attempt_policy"],
    )


METHOD = """# Grafici quantitativi delle campagne complete

Rigenerazione dalla directory `CodiceTesi`:

```sh
python3 scripts/genera_grafici_quantitativi.py
```

Dipendenze: `python3 -m pip install -r scripts/requirements-analysis.txt` (oppure un
ambiente con Matplotlib e NumPy). L'estrazione usa la libreria standard.
Per vedere separatamente i primi tentativi disponibili in archivio:

```sh
python3 scripts/genera_grafici_quantitativi.py --attempt-policy first \\
  --output risultati/primi_tentativi
```

## Campione e confronti

Sono ammesse solo campagne che coprono tutte le 60 celle: 15 casi Hosp e 15 Hosp + ED,
ciascuno con reasoning spento e acceso, con esito `completed` o `failed`.
Una campagna incompleta viene elencata nel riepilogo ed esclusa integralmente.
Le versioni del prompt non costituiscono un criterio di esclusione.
Il campione deve appartenere alla stessa versione di dataset e i casi devono coincidere.

La selezione predefinita prende l'ultima esecuzione archiviata per caso e modalità,
indipendentemente dall'esito. Tutti i tentativi disponibili sono in `tentativi.csv/json`,
con ordine cronologico e indicazione della selezione; non sono ricostruiti tentativi
precedenti che non sono più presenti negli archivi. I confronti principali hanno 30
coppie per modello o modalità. `10_sensibilita_tentativi` confronta esiti e costi delle
politiche primo/ultimo tentativo archiviato. I totali su tutti i tentativi sono separati.
I failed rimangono in tutti i grafici dei costi quando hanno misure disponibili.

Le modalità commutano insieme reasoning e strumenti: i confronti descrivono le due
configurazioni e non isolano un effetto causale del solo reasoning. `acceso` e `spento`
sono le sole etichette del reasoning. Gli esiti sono esclusivamente completati/failed.

## Misure e unità

- Token: contatori completi del provider, aggregati sulle richieste di ogni esecuzione.
  La somma è verificata contro i contatori delle singole richieste. Non si usano stime
  basate sui byte del prompt. Input/output dipendono dal tokenizer del modello: sono
  confronti di consumo dichiarato dal provider, non quantità di testo identiche.
  I token di output comprendono il contenuto interno secondo il contratto del provider;
  non si inferisce una separazione che i contatori non riportano.
- Durata totale: differenza tra fine e inizio della scheda, inclusi i costi applicativi.
  Durata del ciclo e latenze: campi registrati nelle metriche. La velocità è output
  diviso durata del ciclo; non è una velocità di solo decode. Il primo contenuto può
  precedere il primo testo; non viene presentato come primo token visibile.
- Energia GPU: stima in Wh dall'integrale trapezoidale dei campioni `gpu_power_w`
  nell'esatto intervallo della scheda. Interpolazione lineare ai bordi solo se ci sono
  campioni che li racchiudono; nessuna estrapolazione. Gap massimo ammesso: min(20 s,
  due passi nominali del monitor). Si ricalcola lo storico dello snapshot della prova;
  se manca una copertura completa si prova un altro snapshot della stessa campagna.
  La fonte è indicata per ogni esecuzione. Il totale della finestra del monitor non
  viene mai usato come energia della prova. La potenza media è Wh × 3600 / durata.
- Energia per token: Wh GPU × 3600 / token output, sull'intera esecuzione.
  Non misura la sola fase di generazione. Non viene diviso il costo totale per il
  solo numero di casi completati.
- Picco di potenza e temperatura, utilizzo GPU, memoria di sistema e load1 provengono
  dai campioni interni all'intervallo. I picchi sono massimi campionati; non descrivono
  transitori tra campioni. La memoria è memoria di sistema osservata, non VRAM
  allocata esclusivamente dal modello. GiB = byte / 2^30; load1 non è una percentuale CPU.
- L'energia alla presa resta assente se manca `plug_power_w`: l'energia GPU non viene
  presentata come consumo elettrico dell'intero computer.
- Richieste e chiamate tool richieste provengono dalle metriche. I risultati tool
  salvati sono contati solo nei file `risultati_tool.json` presenti. Un file assente
  resta un valore mancante e non dimostra zero esecuzioni tool o fallimento del tool.

## Output e riproducibilità

Figure PNG, SVG e PDF; dati CSV e JSON; differenze e rapporti appaiati per caso;
riepiloghi con n per ogni metrica; hash SHA-256 delle fonti in `fonti.json`.
I CSV lasciano vuote le misure mancanti e i JSON usano null: nessuna imputazione a zero.
I box mostrano Q1–Q3 e mediana, con baffi a 1,5 IQR; tutti i punti sono sovrapposti.
Le differenze sono descrittive, senza test inferenziali o intervalli di confidenza.
Gli assi sono comuni per la stessa metrica nei pannelli di confronto.

Gli script aprono solo `scheda.json`, `metriche.json`, `spark.json` e
`risultati_tool.json` nelle directory delle campagne. Non aprono bozze, lettere,
evidenze o output clinici. Gli export non includono testi o identificativi dei pazienti.
Le schede delle campagne incomplete vengono lette solo per verificarne la copertura.
"""


def sensitivity(data, out):
    attempts = data["attempts"]
    first, last = [], []
    groups = defaultdict(list)
    for row in attempts:
        groups[(row["model"], row["case"], row["reasoning"])].append(row)
    for group in groups.values():
        group = sorted(group, key=lambda row: timestamp(row["started_at"]))
        first.append(group[0])
        last.append(group[-1])
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    order = group_order(first)
    for ax, field, title in zip(
        axes,
        ("completed", "gpu_energy_wh", "total_tokens"),
        ("Casi completati", "Energia GPU stimata totale (Wh)", "Token totali"),
        strict=True,
    ):
        for i, (rows, label, color) in enumerate(
            ((first, "Primo archiviato", "#677a86"), (last, "Ultimo archiviato", "#267f74"))
        ):
            values = []
            for group in order:
                block = [r for r in rows if (r["model"], r["reasoning"]) == group]
                valid = (
                    [r for r in block if finite(r.get(field))] if field != "completed" else block
                )
                value = (
                    sum(r["status"] == "completed" for r in block)
                    if field == "completed"
                    else (sum(r[field] for r in valid) if len(valid) == len(block) else np.nan)
                )
                values.append(value)
            ax.bar(
                np.arange(len(order)) + (i - 0.5) * 0.35,
                values,
                width=0.35,
                label=label,
                color=color,
            )
        ax.set_xticks(range(len(order)), [group_label(g) for g in order], fontsize=8)
        ax.set_title(title)
        ax.set_ylim(bottom=0)
    axes[0].legend(fontsize=8)
    save(
        fig,
        out,
        "10_sensibilita_tentativi",
        "Sensibilità alla scelta dei tentativi archiviati",
        "60 celle per modello in entrambe le selezioni; failed inclusi nei costi. "
        "I tentativi non più presenti nell'archivio non sono ricostruiti.",
    )
    return {"first": summarize(first), "latest": summarize(last)}


def generate(private, output, policy="latest"):
    # Prima estrae e controlla le fonti; non cancella output se i dati non sono coerenti.
    data = collect(private, policy)
    output.mkdir(parents=True, exist_ok=True)
    configure()
    rows = data["selected"]
    outcomes(rows, output)
    distributions(
        rows,
        ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"),
        output,
        "02_token",
        "Consumo di token per esecuzione",
        note="Contatori provider: somma su tutte le richieste del ciclo. "
        "Tokenizer diversi tra modelli; output comprensivo del contenuto interno.",
    )
    distributions(
        rows,
        ("duration_seconds", "cycle_seconds", "first_content_seconds", "first_text_seconds"),
        output,
        "03_tempi",
        "Durate e latenze per esecuzione",
    )
    distributions(
        rows,
        (
            "gpu_energy_wh",
            "gpu_joules_per_output_token",
            "gpu_mean_power_w",
            "gpu_power_w_sample_max",
        ),
        output,
        "04_energia_potenza",
        "Energia stimata e potenza GPU",
        note="Integrale della potenza GPU sul tempo dell'esecuzione. "
        "Energia alla presa non disponibile nel campione corrente.",
    )
    distributions(
        rows,
        (
            "gpu_util_pct_sample_mean",
            "gpu_temp_c_sample_max",
            "system_memory_peak_gib",
            "load1_sample_mean",
        ),
        output,
        "05_hardware",
        "Utilizzo e risorse hardware campionati",
    )
    intra(rows, output)
    inter(rows, output)
    profiles_plot(rows, data["profiles"], output)
    distributions(
        rows,
        (
            "model_requests",
            "tools_requested",
            "saved_tool_results",
            "output_tokens_per_cycle_second",
        ),
        output,
        "09_richieste_tool_velocita",
        "Richieste, tool e velocità del ciclo",
        note="Risultati tool: conteggio degli output salvati; un file assente resta mancante.",
    )
    policies = sensitivity(data, output)
    totals = []
    for model, mode in group_order(data["attempts"]):
        block = [r for r in data["attempts"] if (r["model"], r["reasoning"]) == (model, mode)]
        result = {"model": model, "reasoning": mode, "archived_attempts": len(block)}
        for field in ("gpu_energy_wh", "total_tokens", "duration_seconds"):
            valid = [r[field] for r in block if finite(r.get(field))]
            result[f"{field}_available_sum"] = sum(valid)
            result[f"{field}_available_n"] = len(valid)
            result[f"{field}_complete_total"] = sum(valid) if len(valid) == len(block) else None
        totals.append(result)
    for stem, records in (
        ("esecuzioni", rows),
        ("tentativi", data["attempts"]),
        ("confronti_intramodello", paired_differences(rows, "intra")),
        ("confronti_intermodello", paired_differences(rows, "inter")),
    ):
        write_json(output / f"{stem}.json", records)
        write_csv(output / f"{stem}.csv", records)
    selected_ids = {r["run_id"] for r in rows}
    write_csv(
        output / "campioni_hardware.csv",
        [p for p in data["profiles"] if p["run_id"] in selected_ids],
    )
    write_json(output / "fonti.json", data["sources"])
    write_json(
        output / "riepilogo.json",
        {
            "attempt_policy": policy,
            "analysis_environment": {
                "python": sys.version.split()[0],
                "matplotlib": matplotlib.__version__,
                "numpy": np.__version__,
                "script_sha256": {
                    name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
                    for name in ("campaign_metrics.py", "genera_grafici_quantitativi.py")
                },
            },
            "dataset_version": data["dataset_version"],
            "campaigns": data["campaigns"],
            "excluded_campaigns": data["excluded"],
            "groups": summarize(rows),
            "attempt_sensitivity": policies,
            "all_archived_attempt_totals": totals,
            "selected_runs": len(rows),
            "archived_runs": len(data["attempts"]),
            "recovered_hardware_runs": [
                r["run_id"] for r in data["attempts"] if r["hardware_recovered"]
            ],
            "missing_gpu_energy_runs": [
                r["run_id"] for r in data["attempts"] if r["gpu_energy_wh"] is None
            ],
        },
    )
    (output / "README.md").write_text(METHOD, encoding="utf-8")
    print(f"Generate 10 figure PNG/SVG/PDF in {output}")
    print(
        f"Campagne complete: {len(data['campaigns'])}; esecuzioni selezionate: {len(rows)}; "
        f"tentativi archiviati: {len(data['attempts'])}."
    )
    for group in summarize(rows):
        print(
            f"{group['model']} — reasoning {group['reasoning']}: "
            f"{group['completed']} completati, {group['failed']} failed."
        )
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, default=ROOT.parent / "codice-tesi-private")
    parser.add_argument("--output", type=Path, default=ROOT / "risultati/confronto_campagne")
    parser.add_argument("--attempt-policy", choices=("first", "latest"), default="latest")
    args = parser.parse_args()
    generate(args.private.resolve(), args.output.resolve(), args.attempt_policy)


if __name__ == "__main__":
    main()
