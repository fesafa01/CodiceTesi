"""Copertura delle fonti prima della selezione, senza scrittura in PostgreSQL."""

import re
from collections import defaultdict

from codice_tesi.backend.mimic import (
    MIXED_PATHS,
    during,
    find_discharge_cases,
    followup_order_exclusion,
    followup_signals,
    index_stays,
    linked,
    load_admissions,
    load_poe,
    microbiology_available,
    mixed_eligibility,
    result_comment,
    rows,
)

EXTRA_POE = {
    "ambulatorio_o_clinica": re.compile(r"\b(?:outpatient|clinic)\b", re.I),
    "appuntamento": re.compile(r"\bappointment\b", re.I),
    "invio": re.compile(r"\breferral\b", re.I),
    "rivalutazione": re.compile(r"\b(?:return visit|reassessment|recheck)\b", re.I),
    "istruzioni_dimissione": re.compile(r"\bdischarge instructions\b", re.I),
}


def poe_coverage(hosp, admissions, discharge):
    candidates = {key: admissions[key] for key in discharge}
    current = set()
    extra = {label: set() for label in EXTRA_POE}
    if not candidates:
        return current, extra
    orders, details = load_poe(hosp, candidates)
    for identity, (key, row) in orders.items():
        if followup_order_exclusion(row, admissions[key]) is not None:
            continue
        if followup_signals(row, details[identity]):
            current.add(key)
        parts = (row["order_type"], row["order_subtype"], *details[identity])
        for label, pattern in EXTRA_POE.items():
            if any(pattern.search(" ".join(part.split())) for part in parts):
                extra[label].add(key)
    return current, extra


def source_coverage(root, table, admissions, accepts):
    print(f"Copertura delle fonti: {table}.", flush=True)
    covered = set()
    for key, row in linked(root, table, admissions):
        if key not in covered and accepts(row, admissions[key]):
            covered.add(key)
    return covered


def clinical_coverage(hosp, notes, ed, ed_stays, admissions, *, extended=False):
    radiology = source_coverage(
        notes,
        "radiology",
        admissions,
        lambda row, admission: (
            during(row, admission, "charttime", "storetime")
            and 0 < len(row["text"].strip()) <= 10000
        ),
    )
    procedures = source_coverage(
        hosp,
        "procedures_icd",
        admissions,
        lambda row, admission: (
            row["icd_code"]
            and (
                not row.get("chartdate")
                or admission["admittime"][:10] <= row["chartdate"] <= admission["dischtime"][:10]
            )
        ),
    )
    microbiology = source_coverage(hosp, "microbiologyevents", admissions, microbiology_available)
    ed_stays = {identity: stay for identity, stay in ed_stays.items() if stay[0] in admissions}
    triage = {
        ed_stays[row["subject_id"], row["stay_id"]][0]
        for row in rows(ed, "triage", keys=ed_stays, key_columns=("subject_id", "stay_id"))
        if row["chiefcomplaint"].strip()
    }
    coverage = {
        "motivo_triage_ed": triage,
        "radiologia": radiology,
        "microbiologia": microbiology,
        "procedure_icd": procedures,
    }
    if extended:
        labs = source_coverage(
            hosp,
            "labevents",
            admissions,
            lambda row, admission: (
                (row["value"] or row["valuenum"] or result_comment(row))
                and during(row, admission, "charttime", "storetime")
            ),
        )
        emar = source_coverage(
            hosp,
            "emar",
            admissions,
            lambda row, admission: (
                row["medication"]
                and row["event_txt"]
                and during(row, admission, "charttime", "storetime")
            ),
        )
        coverage.update(
            laboratorio=labs,
            emar_con_stato=emar,
        )

    return coverage


def audit_candidate_coverage(hosp, notes, ed, icu, *, extended=False):
    print("Lettura dei ricoveri, collegamenti ED/ICU e abbinamenti alle lettere.", flush=True)
    admissions = load_admissions(hosp)
    ed_stays = index_stays(ed, "edstays", admissions)
    ed_keys = {key for key, _ in ed_stays.values()}
    icu_keys = {key for key, _ in index_stays(icu, "icustays", admissions).values()}
    services, diagnosis = mixed_eligibility(hosp, notes, admissions)
    admissions = {key: admissions[key] for key in services.keys() & diagnosis.keys()}
    by_path = defaultdict(set)
    for key in admissions:
        by_path[key in ed_keys, key in icu_keys].add(key)

    print("Lettura degli ordini di dimissione verificati.", flush=True)
    discharge = find_discharge_cases(hosp, admissions)
    print("Ricerca dei segnali POE nei ricoveri con terapia alla dimissione.", flush=True)
    current, extra = poe_coverage(hosp, admissions, discharge)

    coverage = clinical_coverage(hosp, notes, ed, ed_stays, admissions, extended=extended)
    radiology = coverage["radiologia"]
    procedures = coverage["procedure_icd"]
    triage = coverage["motivo_triage_ed"]
    new_candidates = set().union(*extra.values()) - current
    criteria = {
        "idonei": set(admissions),
        "terapia": discharge,
        "terapia_poe_attuale": current,
        "terapia_poe_extra_senza_attuale": new_candidates,
        **{f"extra_{label}": keys - current for label, keys in extra.items()},
        **coverage,
        "radiologia_e_procedure": radiology & procedures,
        "triage_radiologia_e_procedure": triage & radiology & procedures,
        "terapia_radiologia_e_procedure": discharge & radiology & procedures,
    }
    if extended:
        criteria["laboratorio_radiologia_emar"] = (
            coverage["laboratorio"] & radiology & coverage["emar_con_stato"]
        )

    print("criterio", "Hosp+ED+ICU", "Hosp+ED", "Hosp+ICU", "Hosp", sep="\t")
    for label, keys in criteria.items():
        print(
            label,
            *(len({subject for subject, _ in keys & by_path[path]}) for path in MIXED_PATHS),
            sep="\t",
        )
    print("Conteggi di pazienti distinti per percorso. Le colonne possono sovrapporsi.")
    print("Le righe extra POE indicano segnali esplorativi, non piani post-dimissione accertati.")
