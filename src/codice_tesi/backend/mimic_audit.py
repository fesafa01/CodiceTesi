"""Confronto locale tra i CSV strutturati e le fonti già importate."""

from collections import Counter, defaultdict
from pathlib import Path

from codice_tesi.backend.database import Database
from codice_tesi.backend.mimic import (
    DISCHARGE_ORDERS,
    FOLLOWUP_ORDERS,
    POE_ORDERS,
    discharge_order_exclusion,
    followup_order_exclusion,
    followup_signals,
    linked,
    load_poe,
)
from codice_tesi.discharge.models import MIMIC_DATASET

COLUMNS = (
    "terapia_csv",
    "terapia_db",
    "terapia_recuperabile",
    "terapia_con_dettagli_recuperabili",
    "poe_candidati_csv",
    "poe_candidati_db",
    "poe_recuperabili",
    "poe_con_dettagli_recuperabili",
)


def imported_orders(sources):
    """Indicizza solo gli ordini presenti nelle fonti strutturate della compilazione."""
    medications = defaultdict(list)
    poe = defaultdict(list)
    for source in sources:
        key = source["patient_id"], source["episode_id"]
        pharmacy = source["origin"] == f"hosp/{DISCHARGE_ORDERS}"
        prefix = "pharmacy_id=" if pharmacy else "poe_id="
        for line in source["text"].splitlines():
            for field in line.split(" | "):
                if field.startswith(prefix):
                    order_id = field.removeprefix(prefix)
                    if pharmacy:
                        medications[key, order_id].append(" ".join(line.split()))
                    else:
                        poe[key, order_id].append(" ".join(line.split()))
                    break
    return medications, poe


def audit_sources(hosp: Path, keys, sources):
    admissions = dict(linked(hosp, "admissions", keys))
    if admissions.keys() != set(keys):
        raise ValueError("Il campione PostgreSQL non coincide con le coppie nei CSV Hosp.")
    counters = {key: Counter() for key in keys}
    reasons = Counter()
    medications, poe = imported_orders(sources)
    prescriptions = {
        (key, row["pharmacy_id"])
        for key, row in linked(hosp, "prescriptions", admissions)
        if row["pharmacy_id"] and row["drug"].strip()
    }
    for key, row in linked(hosp, "pharmacy", admissions):
        if row["proc_type"] != "Discharge Med":
            continue
        order_id = row["pharmacy_id"]
        counts = counters[key]
        counts["terapia_csv"] += 1
        imported = medications[key, order_id]
        reason = discharge_order_exclusion(row, admissions[key])
        if imported:
            counts["terapia_db"] += 1
            if not reason and any(
                row.get(field) and not any(f" | {field}=" in line for line in imported)
                for field in ("route", "doses_per_24_hrs", "disp_sched")
            ):
                counts["terapia_con_dettagli_recuperabili"] += 1
                reasons["terapia: ordine presente, attributi di farmacia assenti"] += 1
            continue
        if reason:
            reasons[f"terapia: {reason}"] += 1
        elif (key, order_id) in prescriptions:
            counts["terapia_recuperabile"] += 1
            reasons["terapia: ordine associato assente nel pacchetto"] += 1
        elif row.get("medication", "").strip():
            counts["terapia_recuperabile"] += 1
            reasons["terapia: ordine nominativo presente solo in pharmacy"] += 1
        else:
            reasons["terapia: farmaco non identificato"] += 1

    print("Farmacia completata. Lettura degli ordini POE e dei loro dettagli.", flush=True)
    orders, details = load_poe(hosp, admissions)
    for identity, (key, row) in orders.items():
        signals = followup_signals(row, details[identity])
        if not signals:
            continue
        counts = counters[key]
        counts["poe_candidati_csv"] += 1
        imported = poe[key, row["poe_id"]]
        if any(" ".join(signal.split()) in line for signal in signals for line in imported):
            counts["poe_candidati_db"] += 1
        if all(
            any(" ".join(detail.split()) in line for line in imported)
            for detail in signals | details[identity]
        ):
            continue
        reason = followup_order_exclusion(row, admissions[key])
        if reason:
            reasons[f"poe: {reason}"] += 1
        elif not imported:
            counts["poe_recuperabili"] += 1
            reasons["poe: ordine assente nel pacchetto"] += 1
        else:
            counts["poe_con_dettagli_recuperabili"] += 1
            reasons["poe: ordine presente, almeno un dettaglio o segnale assente"] += 1
    return counters, reasons


async def audit_mimic(database: Database, hosp: Path, expected: int) -> None:
    async with database.connection(read_only=True) as connection:
        cursor = await connection.execute(
            "SELECT payload->>'patient_id' AS patient_id, payload->>'episode_id' AS episode_id "
            "FROM composition_contexts WHERE payload->>'dataset' = %s ORDER BY id",
            (MIMIC_DATASET,),
        )
        keys = [(row["patient_id"], row["episode_id"]) for row in await cursor.fetchall()]
        if len(keys) != expected or len(set(keys)) != expected or any(not all(key) for key in keys):
            raise ValueError(
                f"Attesi {expected} ricoveri MIMIC distinti in PostgreSQL, trovati {len(keys)}."
            )
        cursor = await connection.execute(
            "SELECT c.payload->>'patient_id' AS patient_id, "
            "c.payload->>'episode_id' AS episode_id, s.payload->>'origin' AS origin, "
            "s.payload->>'text' AS text FROM composition_contexts c "
            "JOIN composition_sources s ON s.context_id = c.id "
            "WHERE c.payload->>'dataset' = %s AND s.payload->>'origin' = ANY(%s)",
            (
                MIMIC_DATASET,
                [f"hosp/{name}" for name in (DISCHARGE_ORDERS, POE_ORDERS, FOLLOWUP_ORDERS)],
            ),
        )
        sources = await cursor.fetchall()

    print(f"Ricoveri MIMIC in PostgreSQL: {len(keys)}. Scansione di cinque CSV Hosp.", flush=True)
    counts, reasons = audit_sources(hosp, keys, sources)
    print("caso", *COLUMNS, sep="\t")
    total = Counter()
    for number, count in enumerate(counts.values(), 1):
        total.update(count)
        print(number, *(count[column] for column in COLUMNS), sep="\t")
    print("TOTALE", *(total[column] for column in COLUMNS), sep="\t")
    print("Motivi delle differenze (ordini):")
    for reason, count in sorted(reasons.items()):
        print(f"  {reason}: {count}")
    print("I candidati POE sono segnali testuali da verificare, non appuntamenti accertati.")
