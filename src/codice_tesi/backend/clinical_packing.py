"""Selezione riproducibile di righe intere dall'estratto clinico completo.

Nessun riassunto LLM e nessuna modifica dei valori. L'estratto completo è
conservato separatamente; questa selezione definisce l'input comune ai modelli.
"""

import hashlib
import re
from collections import defaultdict

PACKING_VERSION = "clinical-whole-records-v2"
LEVELS = (
    {
        "name": "bilanciato",
        "labs": 24,
        "rx": 24,
        "emar": 20,
        "micro": 32,
        "poe": 16,
        "vitals": 12,
        "medrecon": 20,
        "pyxis": 12,
        "radiology": 3,
    },
    {
        "name": "compatto",
        "labs": 16,
        "rx": 16,
        "emar": 12,
        "micro": 24,
        "poe": 10,
        "vitals": 8,
        "medrecon": 16,
        "pyxis": 8,
        "radiology": 2,
    },
    {
        "name": "essenziale",
        "labs": 10,
        "rx": 10,
        "emar": 6,
        "micro": 16,
        "poe": 6,
        "vitals": 6,
        "medrecon": 12,
        "pyxis": 6,
        "radiology": 1,
    },
)


def fields(line):
    result = {}
    for part in line.split(" | "):
        name, separator, value = part.partition("=")
        if separator:
            result.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in result.items()}


def first_last(records, limit):
    if len(records) <= limit:
        return records
    first = limit // 2
    return records[:first] + records[-(limit - first) :]


def grouped_records(records, keys, limit):
    """Prima e ultima osservazione per configurazione, in ordine temporale."""
    groups = defaultdict(list)
    for line in records:
        values = fields(line)
        key = tuple(values.get(name, ()) for name in keys)
        groups[key].append(line)
    # Ogni configurazione riceve una riga prima di assegnarle una seconda riga.
    ordered = sorted(groups.values(), key=lambda values: values[-1])
    selected = first_last([values[-1] for values in ordered], limit)
    for values in ordered:
        if len(selected) == limit:
            break
        if values[-1] in selected and values[0] not in selected:
            selected.append(values[0])
    chosen = set(selected)
    return [line for line in records if line in chosen]


def lab_records(records, limit):
    groups = defaultdict(list)
    for line in records:
        values = fields(line)
        groups[values.get("itemid", (line,))].append(line)

    def rank(values):
        # Flag esplicito e serie ripetuta sono segnali documentali, non nuove diagnosi.
        abnormal = any(fields(line).get("flag") for line in values)
        return -int(len(values) > 1), -int(abnormal), values[-1]

    selected = set()
    for values in sorted(groups.values(), key=rank)[:limit]:
        selected.update((values[0], values[-1]))
        numeric = []
        for line in values:
            match = re.search(r": ([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?: |$)", line)
            if match:
                numeric.append((float(match[1]), line))
        if numeric:
            selected.add(min(numeric)[1])
            selected.add(max(numeric)[1])
    return [line for line in records if line in selected]


def pack_evidence(evidence, level):
    radiology = [source for source in evidence if source.origin.startswith("note/radiology/")]
    # Si conservano referti interi: primo e ultimi, senza tagliare il testo.
    ordered_radio = sorted(radiology, key=lambda source: (source.text[:80], source.id))
    radio_ids = {source.id for source in first_last(ordered_radio, level["radiology"])}
    selected, changes = [], []
    for source in evidence:
        lines = source.text.splitlines()
        header, records = lines[:1], lines[1:]
        origin = source.origin
        chosen = records
        if origin.startswith("note/radiology/"):
            if source.id not in radio_ids:
                changes.append(
                    {
                        "source_id": source.id,
                        "origin": origin,
                        "original_sha256": source.sha256,
                        "included": False,
                        "original_records": 1,
                        "selected_records": 0,
                    }
                )
                continue
        elif origin == "hosp/labevents":
            chosen = lab_records(records, level["labs"])
        elif origin == "hosp/prescriptions":
            chosen = grouped_records(
                records,
                (
                    "drug",
                    "route",
                    "dose_val_rx",
                    "dose_unit_rx",
                    "doses_per_24_hrs",
                    "prod_strength",
                    "form_rx",
                    "status",
                ),
                level["rx"],
            )
        elif origin == "hosp/emar + hosp/emar_detail":
            chosen = grouped_records(
                records,
                (
                    "medication",
                    "event_txt",
                    "pharmacy_id",
                    "dose_given",
                    "dose_given_unit",
                    "dose_due",
                    "infusion_rate",
                ),
                level["emar"],
            )
        elif origin == "hosp/microbiologyevents":
            chosen = grouped_records(
                records,
                (
                    "micro_specimen_id",
                    "test_name",
                    "org_name",
                    "isolate_num",
                    "ab_name",
                    "interpretation",
                    "dilution_text",
                    "commento",
                ),
                level["micro"],
            )
        elif origin == "hosp/poe + hosp/poe_detail":
            chosen = grouped_records(
                records,
                (
                    "order_type",
                    "order_subtype",
                    "transaction_type",
                    "order_status",
                    "field_value",
                ),
                level["poe"],
            )
        elif origin == "ed/vitalsign":
            chosen = first_last(records, level["vitals"])
        elif origin == "ed/medrecon":
            chosen = grouped_records(records, ("name", "gsn", "ndc"), level["medrecon"])
        elif origin == "ed/pyxis":
            chosen = grouped_records(records, ("name", "med_rn"), level["pyxis"])
        # Codici, terapia esplicita di dimissione e candidati follow-up restano completi.
        text = source.text if chosen == records else "\n".join(header + chosen)
        if text != source.text:
            changes.append(
                {
                    "source_id": source.id,
                    "origin": origin,
                    "original_sha256": source.sha256,
                    "included": True,
                    "original_records": len(records),
                    "selected_records": len(chosen),
                }
            )
            source = source.model_copy(
                update={
                    "text": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
        selected.append(source)
    return selected, {
        "version": PACKING_VERSION,
        "level": level["name"],
        "limits": level,
        "original_source_characters": sum(len(s.text) for s in evidence),
        "selected_source_characters": sum(len(s.text) for s in selected),
        "changes": changes,
    }
