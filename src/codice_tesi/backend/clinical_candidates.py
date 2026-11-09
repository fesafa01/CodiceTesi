"""Rosa clinica separata: screening documentale, report e soli INSERT in PostgreSQL.

Le lettere originali servono solo ad abbinare i ricoveri e alla successiva revisione.
Il testo dei riferimenti non interviene in profili, punteggi o copertura CAMPUS.
"""

import csv
import gzip
import hashlib
import json
import logging
import os
import re
import secrets
from collections import Counter, defaultdict
from datetime import date, timedelta

from psycopg.types.json import Jsonb

from codice_tesi.backend.clinical_packing import LEVELS, PACKING_VERSION, pack_evidence
from codice_tesi.backend.mimic import (
    DISCHARGE_ORDERS,
    FOLLOWUP_ORDERS,
    INTENSIVE_CARE_UNIT,
    find_discharge_cases,
    index_stays,
    linked,
    microbiology_available,
    mixed_eligibility,
    pair,
    prepare_mimic,
    rows,
    selected_references,
)
from codice_tesi.discharge.models import MIMIC_DATASET, CompositionPacket, Context, EvidenceSource
from codice_tesi.discharge.template import load_template

PREFIX = "CASO-VALUTAZIONE-CLINICA-"
PROTOCOL = "campus-clinical-candidates-v2"
PROFILES = ("medico_acuto_ed", "infettivologico_ed", "procedurale_hosp")
SURGICAL = {"SURG", "CSURG", "TSURG", "VSURG", "NSURG", "ORTHO", "PSURG", "ENT", "UROL", "GYN"}
MEDICAL = {"MED", "CMED", "NMED"}
ANTIBIOTIC = re.compile(
    r"\b(?:vancomycin|cef\w+|ceph\w+|piperacillin|tazobactam|ampicillin|amoxicillin|"
    r"penicillin|meropenem|ertapenem|imipenem|ciprofloxacin|levofloxacin|moxifloxacin|"
    r"azithromycin|clarithromycin|doxycycline|clindamycin|metronidazole|linezolid|"
    r"daptomycin|gentamicin|tobramycin|trimethoprim|sulfamethoxazole|aztreonam)\b",
    re.I,
)
RECOVERED = {
    "prescriptions": ("stoptime", "drug_type", "prod_strength", "form_rx", "gsn", "ndc"),
    "discharge_med": ("prod_strength", "form_rx", "pharmacy_starttime", "pharmacy_stoptime"),
    "laboratorio": ("ref_range_lower", "ref_range_upper", "priority", "commento"),
    "microbiologia": (
        "test_seq",
        "isolate_num",
        "dilution_text",
        "dilution_comparison",
        "dilution_value",
        "commento",
    ),
    "emar": (
        "dose_due",
        "dose_due_unit",
        "parent_field_ordinal",
        "product_description",
        "prior_infusion_rate",
        "infusion_rate_adjustment",
        "scheduletime",
    ),
    "poe": ("discontinued_by_poe_id",),
    "medrecon": ("gsn", "ndc", "etccode", "etcdescription"),
    "pyxis": ("med_rn", "gsn_rn", "gsn"),
    "transfers": ("outtime",),
}
logger = logging.getLogger(__name__)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


async def existing_contexts(database):
    async with database.connection(read_only=True) as connection:
        cursor = await connection.execute(
            "SELECT id, payload FROM composition_contexts "
            "WHERE payload->>'dataset' = %s ORDER BY id",
            (MIMIC_DATASET,),
        )
        return {row["id"]: row["payload"] for row in await cursor.fetchall()}


def clinical_quotas(limit):
    if not 3 <= limit <= 30:
        raise ValueError("La rosa richiede da 3 a 30 candidati, almeno uno per profilo.")
    return dict(zip(PROFILES, (limit // 3 + (i < limit % 3) for i in range(3)), strict=True))


def screen_candidates(hosp, notes, ed, existing, *, seed, pool_per_profile):
    """Screening sui CSV; nessuna inferenza diagnostica e nessun uso del testo di discharge."""
    excluded = {payload["patient_id"] for payload in existing.values()}
    logger.info("Screening: esclusione dei pazienti già importati, dei decessi e dei passaggi ICU.")
    admissions = {
        pair(row): row
        for row in rows(hosp, "admissions")
        if row["subject_id"] not in excluded
        and row["admittime"]
        and row["admittime"] < row["dischtime"]
        and row.get("hospital_expire_flag", "0") != "1"
        and not row.get("deathtime")
    }
    ed_links = set()
    for row in rows(hosp, "transfers", keys=admissions):
        if INTENSIVE_CARE_UNIT.search(row.get("careunit", "")):
            admissions.pop(pair(row), None)
        elif row.get("eventtype") == "ed":
            ed_links.add(pair(row))
    stays = index_stays(ed, "edstays", admissions, linked_admissions=ed_links)
    ed_keys = {key for key, _ in stays.values()}
    for key in ed_links - ed_keys:
        admissions.pop(key, None)
    services, diagnoses = mixed_eligibility(hosp, notes, admissions)
    admissions = {key: admissions[key] for key in services.keys() & diagnoses.keys()}
    logger.info("Screening: ordini espliciti di dimissione e posologia.")
    complete = set()
    discharge = find_discharge_cases(hosp, admissions, complete_medication=complete)
    admissions = {key: admissions[key] for key in discharge}
    procedures = {
        key
        for key, row in linked(hosp, "procedures_icd", admissions)
        if row["icd_code"]
        and (
            not row.get("chartdate")
            or admissions[key]["admittime"][:10]
            <= row["chartdate"]
            <= admissions[key]["dischtime"][:10]
        )
    }
    logger.info("Screening: microbiologia disponibile, campioni positivi e antibiotici prescritti.")
    positive, susceptibility = set(), set()
    for key, row in linked(hosp, "microbiologyevents", admissions):
        if microbiology_available(row, admissions[key]) and row.get("org_name", "").strip():
            positive.add(key)
            if row.get("ab_name") and row.get("interpretation"):
                susceptibility.add(key)
    antibiotics = {
        key
        for key, row in linked(hosp, "prescriptions", positive)
        if ANTIBIOTIC.search(row.get("drug", ""))
        and admissions[key]["admittime"] <= row["starttime"] <= admissions[key]["dischtime"]
    }
    infection = positive & susceptibility & antibiotics & ed_keys
    triage = {
        stays[row["subject_id"], row["stay_id"]][0]
        for row in rows(ed, "triage", keys=stays, key_columns=("subject_id", "stay_id"))
        if row.get("chiefcomplaint", "").strip()
    }
    pools = {profile: [] for profile in PROFILES}
    metadata = {}
    for key in admissions:
        service = services[key][1]
        if key in infection and key in triage:
            profile = PROFILES[1]
        elif key in ed_keys and key in triage and service in MEDICAL:
            profile = PROFILES[0]
        elif key not in ed_keys and service in SURGICAL and key in procedures:
            profile = PROFILES[2]
        else:
            continue
        score = 3 * (key in complete) + 2 * (key in procedures) + (key in triage)
        rank = hashlib.sha256(f"{seed}:{key[0]}:{key[1]}".encode()).hexdigest()
        pools[profile].append((-score, rank, key))
        metadata[key] = {
            "profile": profile,
            "initial_service": service,
            "primary_icd_category": ":".join(diagnoses[key]),
            "screening_positive_culture": key in positive,
            "screening_susceptibility": key in susceptibility,
            "screening_antibiotic_order": key in antibiotics,
        }
    counts = {profile: len(values) for profile, values in pools.items()}
    selected = []
    for profile, values in pools.items():
        used = set()
        for _, _, key in sorted(values):
            if key[0] in used:
                continue
            used.add(key[0])
            selected.append(key)
            if len(used) == pool_per_profile:
                break
        logger.info(
            "Profilo %s: %s ricoveri eleggibili; %s pazienti nel pool.",
            profile,
            counts[profile],
            len(used),
        )
    chosen = set(selected)
    return (
        {key: admissions[key] for key in selected},
        {identity: stay for identity, stay in stays.items() if stay[0] in chosen},
        {key: metadata[key] for key in selected},
        counts,
    )


def add_historical_omr(hosp, admissions, contexts, sources, *, lookback_days=365):
    """Ultima misura per tipo, precedente al giorno di ingresso; OMR non ha hadm_id."""
    by_subject = defaultdict(list)
    for key, admission in admissions.items():
        upper = date.fromisoformat(admission["admittime"][:10])
        by_subject[key[0]].append((key, upper - timedelta(days=lookback_days), upper))
    observations = defaultdict(dict)
    path = hosp / "omr.csv.gz"
    if not path.exists():
        raise ValueError("OMR assente: la rosa clinica richiede anche hosp/omr.csv.gz.")
    for row in rows(hosp, "omr"):
        if row["subject_id"] not in by_subject or not row["result_value"].strip():
            continue
        recorded = date.fromisoformat(row["chartdate"])
        for key, lower, upper in by_subject[row["subject_id"]]:
            if lower <= recorded < upper:
                label = row["result_name"]
                rank = row["chartdate"], int(row["seq_num"])
                if label not in observations[key] or rank > observations[key][label][0]:
                    observations[key][label] = rank, row
    for context in contexts:
        key = context.patient_id, context.episode_id
        if not observations[key]:
            continue
        lines = [
            "OMR storico del paziente: misure nei 365 giorni precedenti il giorno di ingresso. "
            "Nessun legame esplicito con il ricovero; non descrivono lo stato alla dimissione."
        ]
        for _, row in sorted(
            observations[key].values(), key=lambda item: (item[0], item[1]["result_name"])
        ):
            lines.append(
                " | ".join(
                    f"{name}={row[name]}"
                    for name in ("chartdate", "seq_num", "result_name", "result_value")
                )
            )
        text = "\n".join(lines)
        sources.append(
            EvidenceSource(
                id=f"{context.id}-fonte-omr",
                context_id=context.id,
                kind="contesto_storico",
                text=text,
                origin="hosp/omr (storico pre-ricovero)",
                version=context.dataset_version,
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
        )


def input_estimates(context, evidence):
    from codice_tesi.config import GEMMA_MODEL, MISTRAL_MODEL, QWEN_MODEL, ModelConfig
    from codice_tesi.discharge.config import ContextConfig
    from codice_tesi.discharge.context import prepare_request

    packet = CompositionPacket(
        context=context,
        sources=tuple(sorted(evidence, key=lambda s: s.id)),
        template=load_template(),
    )
    estimates = {}
    for name in (QWEN_MODEL, GEMMA_MODEL, MISTRAL_MODEL):
        model = ModelConfig(model=name, thinking=True, tools_enabled=True)
        request = prepare_request(
            packet, ContextConfig(), output_tokens=model.max_tokens, model_config=model
        )
        estimates[name] = {
            "estimate": request.input_tokens_estimate,
            "method": request.input_estimate_method,
            "tokenizer_revision": request.input_tokenizer_revision,
        }
    return estimates


def campus_coverage(evidence):
    """Disponibilità documentale prudenziale; non è un giudizio di completezza clinica."""
    groups = defaultdict(list)
    for source in evidence:
        groups[source.origin].append(source)

    def ids(*origins):
        return [source.id for origin in origins for source in groups[origin]]

    all_ids = [source.id for source in evidence]
    radiology = [source.id for source in evidence if source.origin.startswith("note/radiology/")]
    admin = ids("hosp/admissions + hosp/patients")
    diagnostic = ids("hosp/diagnoses_icd")
    procedural = ids("hosp/procedures_icd")
    labs = ids("hosp/labevents")
    discharge = groups[f"hosp/{DISCHARGE_ORDERS}"]
    med_lines = [
        line
        for source in discharge
        for line in source.text.splitlines()
        if "Ordine di farmacia per dimissione" in line
    ]
    complete = sum(
        all(f"{name}=" in line for name in ("drug", "dose_val_rx", "dose_unit_rx", "route"))
        and ("frequency=" in line or "doses_per_24_hrs=" in line)
        for line in med_lines
    )
    followup = ids(f"hosp/{FOLLOWUP_ORDERS}")
    lab_series = defaultdict(set)
    for source in groups["hosp/labevents"]:
        for line in source.text.splitlines():
            item = re.search(r" \| itemid=([^ |]+)", line)
            if item:
                lab_series[item[1]].add(line.split(" | ", 1)[0])
    mapping = {
        "identificazione": (
            "parziale",
            admin,
            "Identificativi deidentificati; struttura CAMPUS assente.",
        ),
        "data_dimissione": ("documentato", admin, "Data amministrativa effettiva."),
        "diagnosi_dimissione": (
            "parziale",
            diagnostic,
            "Codifica retrospettiva; non diagnosi narrativa finale.",
        ),
        "diagnosi_codificate": ("documentato", diagnostic, "Codifica amministrativa post-hoc."),
        "procedure_codificate": ("documentato", procedural, "Codici delle procedure, se presenti."),
        "motivo_ricovero": (
            "parziale",
            ids("ed/triage"),
            "Chief complaint ED; indicazione al ricovero da verificare.",
        ),
        "problemi_non_attivi": (
            "da_verificare",
            radiology,
            "Nessuna risoluzione dedotta dai codici ICD.",
        ),
        "procedure_diagnostiche_terapeutiche": (
            "parziale",
            procedural + radiology,
            "Distinguere ordine, procedura ed esito.",
        ),
        "esami_laboratorio": ("documentato", labs, "Valori, unità, date e commenti disponibili."),
        "decorso": (
            "parziale",
            [i for i in all_ids if i not in admin],
            "Eventi strutturati e referti; causalità e risposta clinica da verificare.",
        ),
        "condizioni_dimissione": (
            "parziale",
            admin,
            "Sola destinazione amministrativa; stato clinico non attestato.",
        ),
        "programma_followup": (
            "candidato_da_verificare",
            followup,
            "Segnale POE; verificare indicazione, destinatario e tempistica.",
        ),
        "terapia_dimissione": (
            "parziale",
            [s.id for s in discharge],
            "Singoli ordini espliciti; nessuna garanzia di terapia finale completa.",
        ),
        "contatti_urgenze": (
            "da_verificare",
            followup,
            "Recapiti e istruzioni richiedono verifica testuale.",
        ),
        "istologico_in_attesa": (
            "da_verificare",
            [],
            "Biopsia o esame mancante non implicano attesa istologica.",
        ),
        "informazioni_istologico": (
            "condizionale",
            [],
            "Applicabile solo con attesa istologica esplicita.",
        ),
        "medici": ("escluso", [], "Nomi deidentificati; campo escluso dalla valutazione MIMIC."),
    }
    coverage = {}
    for section in load_template().sections:
        state, references, note = mapping[section.id]
        if not references and state in {"documentato", "parziale", "candidato_da_verificare"}:
            state = "non_documentato"
        coverage[section.id] = {
            "title": section.title,
            "status": state,
            "source_ids": references,
            "note": note,
        }
    return coverage, {
        "discharge_orders": len(med_lines),
        "orders_with_dose_route_frequency": complete,
        "orders_with_explicit_duration": sum("duration=" in line for line in med_lines),
        "has_radiology": bool(radiology),
        "has_microbiology": bool(ids("hosp/microbiologyevents")),
        "has_emar": bool(ids("hosp/emar + hosp/emar_detail")),
        "has_labs": bool(labs),
        "lab_items_with_repeated_measurements": sum(
            len(times) >= 2 for times in lab_series.values()
        ),
        "has_followup_candidate": bool(followup),
        "has_historical_omr": bool(ids("hosp/omr (storico pre-ricovero)")),
    }


def recovered_columns(evidence):
    origins = {
        "prescriptions": "hosp/prescriptions",
        "discharge_med": f"hosp/{DISCHARGE_ORDERS}",
        "laboratorio": "hosp/labevents",
        "microbiologia": "hosp/microbiologyevents",
        "emar": "hosp/emar + hosp/emar_detail",
        "poe": "hosp/poe + hosp/poe_detail",
        "medrecon": "ed/medrecon",
        "pyxis": "ed/pyxis",
        "transfers": "hosp/transfers",
    }
    result = {}
    for group, columns in RECOVERED.items():
        lines = [
            line
            for source in evidence
            if source.origin == origins[group]
            for line in source.text.splitlines()
        ]
        result[group] = {
            column: sum(
                bool(re.search(r"(?:^| \| )" + re.escape(column) + "=", line)) for line in lines
            )
            for column in columns
        }
    return result


def select_shortlist(
    contexts,
    sources,
    references,
    metadata,
    *,
    limit,
    minimum,
    maximum,
    seed,
    audit=None,
    source_profile="complete",
):
    quotas = clinical_quotas(limit)
    by_context = defaultdict(list)
    for source in sources:
        by_context[source.context_id].append(source)
    ranked = defaultdict(list)
    rejected = Counter()
    for context in contexts:
        full_evidence = sorted(by_context[context.id], key=lambda source: source.id)
        evidence = full_evidence
        info = metadata[context.patient_id, context.episode_id]
        coverage, metrics = campus_coverage(evidence)
        full_coverage, full_metrics = coverage, metrics
        # Un pool ricco deve consentire almeno esami seriali e una terapia esplicita.
        if not metrics["lab_items_with_repeated_measurements"] or not metrics["has_emar"]:
            rejected["senza_laboratorio_o_emar"] += 1
            if audit is not None:
                audit.append(
                    {
                        **info,
                        **metrics,
                        "context_id": context.id,
                        "exclusion": "senza_laboratorio_seriale_o_emar",
                        "campus": coverage,
                    }
                )
            continue
        attempts = []
        packing = None
        levels = LEVELS if source_profile == "clinical" else (None,)
        for level in levels:
            current = context
            if level is not None:
                evidence, packing = pack_evidence(full_evidence, level)
                current = context.model_copy(
                    update={
                        "limitations": tuple(
                            note
                            for note in context.limitations
                            if not note.startswith("Archivio esteso degli eventi")
                        )
                        + (
                            f"Input clinico selezionato per righe intere, profilo {level['name']}. "
                            "L'estratto completo è conservato separatamente. "
                            "L'assenza di una riga nell'input non dimostra assenza dell'evento.",
                        )
                    }
                )
            estimates = input_estimates(current, evidence)
            values = [value["estimate"] for value in estimates.values()]
            attempts.append(
                {"level": level["name"] if level else "complete", "input_estimates": estimates}
            )
            if minimum <= min(values) and max(values) <= maximum:
                break
        coverage, metrics = campus_coverage(evidence)
        score = (
            3 * metrics["orders_with_dose_route_frequency"]
            + 2 * metrics["orders_with_explicit_duration"]
            + 3 * metrics["has_radiology"]
            + 2 * metrics["has_microbiology"]
            + 3 * metrics["has_followup_candidate"]
            + metrics["has_historical_omr"]
            + sum(value["status"] == "documentato" for value in coverage.values())
        )
        report = {
            **info,
            **metrics,
            "patient_id": context.patient_id,
            "episode_id": context.episode_id,
            "context_id": context.id,
            "score": score,
            "input_estimates": estimates,
            "campus": coverage,
            "recovered_columns": recovered_columns(evidence),
            "source_count": len(evidence),
            "source_characters": sum(len(s.text) for s in evidence),
            "full_campus": full_coverage,
            "full_metrics": full_metrics,
            "full_recovered_columns": recovered_columns(full_evidence),
            "packing": packing,
            "packing_attempts": attempts,
        }
        exclusion = (
            "sotto_floor"
            if min(values) < minimum
            else "sopra_ceiling"
            if max(values) > maximum
            else None
        )
        if audit is not None:
            audit.append({**report, "exclusion": exclusion})
        if exclusion:
            rejected[exclusion] += 1
            continue
        tie = hashlib.sha256(
            f"{seed}:{context.patient_id}:{context.episode_id}".encode()
        ).hexdigest()
        ranked[info["profile"]].append((-score, tie, context.id, current, evidence, report))
    selected, selected_sources, selected_refs, reports = [], [], {}, []
    used = set()
    for profile, quota in quotas.items():
        count = 0
        for _, _, _, context, evidence, report in sorted(ranked[profile]):
            if context.patient_id in used:
                continue
            used.add(context.patient_id)
            label = f"{PREFIX}{len(selected) + 1:02}"
            selected.append(context.model_copy(update={"display_name": label}))
            selected_sources.extend(evidence)
            selected_refs[context.id] = references[context.id]
            reports.append({**report, "label": label})
            count += 1
            if count == quota:
                break
        if count != quota:
            raise ValueError(
                f"Rosa insufficiente per {profile}: {count}/{quota} nel range {minimum}–{maximum}. "
                f"Scarti: {dict(rejected)}. Aumentare --pool-per-profile o rivedere le soglie. "
                "Nessuna aggiunta al database."
            )
    digest = fingerprint(
        [
            PROTOCOL,
            seed,
            minimum,
            maximum,
            [c.model_dump(mode="json") for c in selected],
            [(s.id, s.sha256) for s in selected_sources],
        ]
    )
    selected = [c.model_copy(update={"dataset_version": digest}) for c in selected]
    selected_sources = [s.model_copy(update={"version": digest}) for s in selected_sources]
    return selected, selected_sources, selected_refs, reports, dict(rejected)


async def append_candidates(database, contexts, sources, references, *, expected):
    """Transazione dedicata senza DELETE, UPDATE o UPSERT sui dati già importati."""
    if not contexts or len({c.id for c in contexts}) != len(contexts):
        raise ValueError("Rosa vuota o identificativi duplicati.")
    if len({c.patient_id for c in contexts}) != len(contexts):
        raise ValueError("La rosa deve contenere pazienti distinti.")
    if set(references) != {c.id for c in contexts} or any(
        not notes for notes in references.values()
    ):
        raise ValueError("Riferimenti mancanti nella rosa clinica.")
    if len({c.dataset_version for c in contexts}) != 1:
        raise ValueError("Versioni discordanti nella rosa clinica.")
    if len({s.id for s in sources}) != len(sources):
        raise ValueError("Fonti duplicate nella rosa clinica.")
    for context in contexts:
        if context.dataset != MIMIC_DATASET or not (context.display_name or "").startswith(PREFIX):
            raise ValueError("Importazione riservata ai candidati clinici MIMIC.")
        evidence = tuple(
            sorted((s for s in sources if s.context_id == context.id), key=lambda s: s.id)
        )
        CompositionPacket(context=context, sources=evidence, template=load_template())
        if any(s.version != context.dataset_version for s in evidence):
            raise ValueError("Versione delle fonti discordante.")
    if any(s.context_id not in references for s in sources):
        raise ValueError("Fonte fuori dalla rosa clinica.")
    async with database.connection() as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(183627411)")
        cursor = await connection.execute(
            "SELECT id, payload FROM composition_contexts "
            "WHERE payload->>'dataset' = %s ORDER BY id",
            (MIMIC_DATASET,),
        )
        current = {row["id"]: row["payload"] for row in await cursor.fetchall()}
        if current != expected:
            raise ValueError(
                "Il campione presente è cambiato durante lo screening; ripetere la verifica."
            )
        old_patients = {payload["patient_id"] for payload in current.values()}
        if old_patients & {c.patient_id for c in contexts}:
            raise ValueError("Un candidato appartiene a un paziente già presente.")
        if set(current) & {c.id for c in contexts}:
            raise ValueError("Identificativo di ricovero già presente.")
        async with connection.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO composition_contexts (id, payload) VALUES (%s, %s)",
                [(c.id, Jsonb(c.model_dump(mode="json"))) for c in contexts],
            )
            await cursor.executemany(
                "INSERT INTO composition_sources (id, context_id, payload) VALUES (%s, %s, %s)",
                [(s.id, s.context_id, Jsonb(s.model_dump(mode="json"))) for s in sources],
            )
            await cursor.executemany(
                "INSERT INTO mimic_discharge_references (context_id, notes) VALUES (%s, %s)",
                [(identifier, Jsonb(notes)) for identifier, notes in references.items()],
            )


def private_write(path, text):
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(text)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def pool_configuration(args, baseline):
    inventory = []
    for module, root in (("hosp", args.hosp), ("note", args.notes), ("ed", args.ed)):
        for path in sorted(root.glob("*.csv.gz")):
            stat = path.stat()
            inventory.append([module, path.name, stat.st_size, stat.st_mtime_ns])
    return {
        "extraction": "expanded-clinical-v1",
        "seed": args.seed,
        "pool_per_profile": args.pool_per_profile,
        "baseline_fingerprint": fingerprint(baseline),
        "inventory": inventory,
    }


def previous_pool(args, existing):
    """Riprende i soli identificativi già sottoposti allo screening fallito.

    Le fonti e gli abbinamenti sono estratti nuovamente dai CSV. Non si promuove
    alcun risultato del vecchio report a dato clinico.
    """
    path = args.report_dir / "screening-pool.json"
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    if any(
        report.get(name) != value
        for name, value in (
            ("seed", args.seed),
            ("pool_per_profile", args.pool_per_profile),
            ("baseline_fingerprint", fingerprint(existing)),
        )
    ):
        return None
    if report.get("protocol") not in {"campus-clinical-candidates-v1", PROTOCOL}:
        return None
    records = {row["context_id"]: row for row in report.get("pool", [])}
    if not records:
        return None
    excluded = {payload["patient_id"] for payload in existing.values()}
    admissions, metadata = {}, {}
    for row in rows(args.hosp, "admissions"):
        identifier = f"mimic-{row['hadm_id']}"
        if identifier not in records:
            continue
        if (
            row["subject_id"] in excluded
            or row.get("hospital_expire_flag") == "1"
            or row.get("deathtime")
        ):
            return None
        key = pair(row)
        admissions[key] = row
        metadata[key] = {
            name: value
            for name, value in records[identifier].items()
            if name
            in {
                "profile",
                "initial_service",
                "primary_icd_category",
                "screening_positive_culture",
                "screening_susceptibility",
                "screening_antibiotic_order",
            }
        }
    if len(admissions) != len(records):
        return None
    for key, row in linked(args.hosp, "transfers", admissions):
        if INTENSIVE_CARE_UNIT.search(row.get("careunit", "")):
            return None
    stays = index_stays(args.ed, "edstays", admissions)
    ed_keys = {key for key, _ in stays.values()}
    if any(
        (meta["profile"] != "procedurale_hosp") != (key in ed_keys)
        for key, meta in metadata.items()
    ):
        return None
    backup = args.report_dir / "screening-pool-prima-packing.json"
    if not backup.exists():
        private_write(backup, path.read_text())
    logger.info(
        "Riutilizzo dei %s ricoveri del pool già esaminato; nuova estrazione delle fonti.",
        len(admissions),
    )
    return (
        admissions,
        stays,
        metadata,
        {
            "reused_previous_pool": True,
            "pool_sizes": dict(Counter(meta["profile"] for meta in metadata.values())),
        },
    )


def full_pool(args, baseline):
    config = pool_configuration(args, baseline)
    path = args.report_dir / "pool-completo-privato.json.gz"
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as file:
            saved = json.load(file)
        if saved["config"] != config:
            raise ValueError(
                "Il pool salvato usa dati o criteri diversi: usare una nuova --report-dir."
            )
        logger.info("Lettura dell'estratto completo già salvato: nessuna nuova scansione dei CSV.")
        contexts = [Context.model_validate(row) for row in saved["contexts"]]
        sources = [EvidenceSource.model_validate(row) for row in saved["sources"]]
        metadata = {(c.patient_id, c.episode_id): saved["metadata"][c.id] for c in contexts}
        return contexts, sources, saved["references"], metadata, saved["counts"]
    screened = previous_pool(args, baseline)
    if screened is None:
        screened = screen_candidates(
            args.hosp,
            args.notes,
            args.ed,
            baseline,
            seed=args.seed,
            pool_per_profile=args.pool_per_profile,
        )
    admissions, stays, metadata, counts = screened
    if not admissions:
        raise ValueError("Nessun candidato per i profili richiesti.")
    logger.info(
        "Estrazione completa di %s ricoveri nel pool; salvataggio privato prima della selezione.",
        len(admissions),
    )
    paired = selected_references(args.notes, admissions)
    contexts, sources, references = prepare_mimic(
        args.hosp,
        args.notes,
        admissions,
        paired,
        seed=args.seed,
        ed=args.ed,
        ed_stays=stays,
        rich_pilot=True,
        clinical_details=True,
    )
    add_historical_omr(args.hosp, admissions, contexts, sources)
    saved = {
        "config": config,
        "contexts": [c.model_dump(mode="json") for c in contexts],
        "sources": [s.model_dump(mode="json") for s in sources],
        "references": references,
        "metadata": {c.id: metadata[c.patient_id, c.episode_id] for c in contexts},
        "counts": counts,
    }
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                compressed.write(json.dumps(saved, ensure_ascii=False).encode())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return contexts, sources, references, metadata, counts


def access_update(path, new_ids, *, write=False):
    """Conserva il token e tutte le assegnazioni preesistenti, senza eseguire il file env."""
    original = path.read_text()
    tokens = re.findall(r"^APP_MIMIC_TOKEN=([A-Za-z0-9_-]{43,})$", original, re.M)
    assignments = re.findall(r"^APP_MIMIC_CONTEXTS=([^\r\n]+)$", original, re.M)
    if len(tokens) != 1 or len(assignments) != 1:
        raise ValueError(
            "Il file di accesso deve contenere un token e un elenco di assegnazioni validi."
        )
    ids = set(assignments[0].split(",")) | set(new_ids)
    if not all(re.fullmatch(r"mimic-[0-9]+", identifier) for identifier in ids):
        raise ValueError("Identificativi non validi nel file di accesso.")
    if write:
        updated = re.sub(
            r"^APP_MIMIC_CONTEXTS=[^\r\n]+$",
            "APP_MIMIC_CONTEXTS=" + ",".join(sorted(ids)),
            original,
            flags=re.M,
        )
        private_write(path, updated)


def write_reports(directory, report):
    private_write(
        directory / "candidati.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    lines = [
        "# Rosa per la valutazione clinica CAMPUS",
        "",
        f"Stato: **{report['status']}**. Protocollo `{PROTOCOL}`.",
        "",
        "Profili e copertura sono uno screening documentale da verificare clinicamente. "
        "I tre casi definitivi non sono ancora selezionati.",
        "",
        "Il floor si applica al minimo delle tre stime; il ceiling al massimo. "
        "Qwen usa il tokenizer locale; Gemma/Mistral usano byte UTF-8 prudenziali, "
        "non token reali. Le stime includono il prompt con tool; non includono gli schemi tool "
        "né il loro uso successivo. "
        "Riservare separatamente output, margine e risultati tool nella finestra del serving.",
        "",
        "L'archivio completo è conservato in pool-completo-privato.json.gz. L'input dei modelli "
        "può essere una selezione deterministica di righe intere. Il JSON distingue copertura "
        "e attributi nell'archivio completo da quelli nell'input. Nessun valore o referto "
        "è accorciato all'interno di una riga o di un documento.",
        "",
        "Antibiotico prescritto e coltura positiva non dimostrano un nesso causale. "
        "I tempi di stop sono quelli documentati nell'ordine, anche pianificati; non dimostrano "
        "l'istante in cui una modifica è diventata nota. OMR è storico e privo di hadm_id.",
        "",
        "| Caso | Profilo | Servizio | Ordini dimissione | Con dose/via/frequenza | Radiologia | "
        "Micro | Candidato follow-up | Stima min–max |",
        "|---|---|---|---:|---:|---|---|---|---:|",
    ]
    for candidate in report["candidates"]:
        values = [v["estimate"] for v in candidate["input_estimates"].values()]
        lines.append(
            f"| {candidate['label']} | {candidate['profile']} | {candidate['initial_service']} | "
            f"{candidate['discharge_orders']} | {candidate['orders_with_dose_route_frequency']} | "
            f"{candidate['has_radiology']} | {candidate['has_microbiology']} | "
            f"{candidate['has_followup_candidate']} | {min(values)}–{max(values)} |"
        )
    for candidate in report["candidates"]:
        lines.extend(
            [
                "",
                f"## {candidate['label']}",
                "",
                f"Contesto `{candidate['context_id']}`; profilo `{candidate['profile']}`.",
                "",
                "| Campo CAMPUS | Archivio completo | Input selezionato | Nota |",
                "|---|---|---|---|",
            ]
        )
        for identifier, field in candidate["campus"].items():
            full_status = candidate.get("full_campus", candidate["campus"])[identifier]["status"]
            lines.append(
                f"| {field['title']} | {full_status} | {field['status']} | {field['note']} |"
            )
        packing = candidate.get("packing")
        if packing:
            lines.extend(
                [
                    "",
                    f"Profilo di selezione: **{packing['level']}**. "
                    f"Caratteri delle fonti: {packing['selected_source_characters']} "
                    f"su {packing['original_source_characters']} nell'archivio completo.",
                    "",
                    "| Fonte | Righe/referti nell'archivio | Righe/referti nell'input |",
                    "|---|---:|---:|",
                ]
            )
            for change in packing["changes"]:
                lines.append(
                    f"| {change['source_id']} | {change['original_records']} | "
                    f"{change['selected_records']} |"
                )
        lines.extend(["", "Attributi aggiuntivi presenti nell'input selezionato:", ""])
        for group, columns in candidate["recovered_columns"].items():
            present = ", ".join(f"`{name}`: {count}" for name, count in columns.items() if count)
            if present:
                lines.append(f"- {group}: {present}.")
    private_write(directory / "candidati.md", "\n".join(lines) + "\n")
    temporary = directory / f".copertura.{secrets.token_hex(8)}"
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", newline=""
        ) as f:
            writer = csv.writer(f)
            writer.writerow(
                (
                    "caso",
                    "profilo",
                    "campo",
                    "disponibilita_input",
                    "disponibilita_archivio",
                    "fonti_input",
                    "nota",
                )
            )
            for candidate in report["candidates"]:
                for field, coverage in candidate["campus"].items():
                    writer.writerow(
                        (
                            candidate["label"],
                            candidate["profile"],
                            field,
                            coverage["status"],
                            candidate.get("full_campus", candidate["campus"])[field]["status"],
                            ";".join(coverage["source_ids"]),
                            coverage["note"],
                        )
                    )
        temporary.replace(directory / "copertura-campus.csv")
    finally:
        temporary.unlink(missing_ok=True)


async def run_clinical_candidates(database, args):
    quotas = clinical_quotas(args.limit)
    if not 0 <= args.min_input_tokens <= args.max_input_tokens or args.max_input_tokens < 1:
        raise ValueError("Usare 0 <= floor <= ceiling, con ceiling positivo.")
    if args.seed < 0 or args.pool_per_profile < max(quotas.values()):
        raise ValueError("Seed non negativo e pool per profilo almeno pari alla quota maggiore.")
    access_update(args.access_file, ())  # Verifica prima delle scansioni e della transazione.
    existing = await existing_contexts(database)
    clinical = {
        key: value
        for key, value in existing.items()
        if (value.get("display_name") or "").startswith(PREFIX)
    }
    baseline = {key: value for key, value in existing.items() if key not in clinical}
    if len(baseline) != args.expect_existing:
        raise ValueError(f"Attesi {args.expect_existing} casi precedenti; trovati {len(baseline)}.")
    args.report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    selection_path = args.report_dir / "selezione-privata.json"
    config = {
        "protocol": PROTOCOL,
        "limit": args.limit,
        "seed": args.seed,
        "floor": args.min_input_tokens,
        "ceiling": args.max_input_tokens,
        "pool_per_profile": args.pool_per_profile,
        "baseline_fingerprint": fingerprint(baseline),
        "source_profile": getattr(args, "source_profile", "clinical"),
        "packing_version": PACKING_VERSION,
    }
    if selection_path.exists():
        saved = json.loads(selection_path.read_text())
        if saved["config"] != config:
            raise ValueError(
                "Report già presente con parametri diversi: usare una nuova --report-dir."
            )
        contexts = [Context.model_validate(row) for row in saved["contexts"]]
        sources = [EvidenceSource.model_validate(row) for row in saved["sources"]]
        references, report = saved["references"], saved["report"]
    else:
        if clinical:
            raise ValueError(
                "Una rosa clinica è già presente: riusare la sua --report-dir per riprendere."
            )
        contexts, sources, references, metadata, counts = full_pool(args, baseline)
        logger.info("Stima locale degli input e copertura CAMPUS; nessuna chiamata ai modelli.")
        audit = []
        try:
            contexts, sources, references, reports, rejected = select_shortlist(
                contexts,
                sources,
                references,
                metadata,
                limit=args.limit,
                minimum=args.min_input_tokens,
                maximum=args.max_input_tokens,
                seed=args.seed,
                audit=audit,
                source_profile=config["source_profile"],
            )
        finally:
            private_write(
                args.report_dir / "screening-pool.json",
                json.dumps({**config, "pool": audit}, ensure_ascii=False, indent=2) + "\n",
            )
        report = {
            **config,
            "status": "preparato",
            "screening_counts": counts,
            "rejected_in_pool": rejected,
            "candidates": reports,
        }
        saved = {
            "config": config,
            "contexts": [c.model_dump(mode="json") for c in contexts],
            "sources": [s.model_dump(mode="json") for s in sources],
            "references": references,
            "report": report,
        }
        private_write(selection_path, json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
    write_reports(args.report_dir, report)
    if getattr(args, "prepare_only", False):
        print(
            f"Rosa preparata e verificata: {len(contexts)} candidati. Nessuna aggiunta al database."
        )
        print(f"Report: {args.report_dir / 'candidati.md'}")
        return
    if clinical:
        if set(clinical) != {c.id for c in contexts} or any(
            clinical[c.id] != c.model_dump(mode="json") for c in contexts
        ):
            raise ValueError("La rosa già presente non coincide con la selezione privata.")
        # Confronto anche fonti e riferimenti prima di considerare concluso un riavvio.
        async with database.connection(read_only=True) as connection:
            cursor = await connection.execute(
                "SELECT id, payload FROM composition_sources WHERE context_id = ANY(%s)",
                (list(clinical),),
            )
            stored = {r["id"]: r["payload"] for r in await cursor.fetchall()}
            cursor = await connection.execute(
                "SELECT context_id, notes FROM mimic_discharge_references "
                "WHERE context_id = ANY(%s)",
                (list(clinical),),
            )
            stored_refs = {r["context_id"]: r["notes"] for r in await cursor.fetchall()}
        if (
            stored != {s.id: s.model_dump(mode="json") for s in sources}
            or stored_refs != references
        ):
            raise ValueError(
                "Fonti o riferimenti della rosa presente differiscono dall'export privato."
            )
    else:
        await append_candidates(database, contexts, sources, references, expected=existing)
    access_update(args.access_file, (c.id for c in contexts), write=True)
    report["status"] = "aggiunto"
    write_reports(args.report_dir, report)
    print(
        f"Rosa clinica: {len(contexts)} candidati aggiunti; "
        f"{len(baseline)} casi precedenti conservati."
    )
    print(f"Report: {args.report_dir / 'candidati.md'}")
    print("Accesso aggiornato conservando il token. Ricreare i servizi che leggono access.env.")
