import asyncio
import csv
import gzip
import json
from collections import defaultdict

import pytest

from codice_tesi.backend.database import Database
from codice_tesi.backend.mimic import add_icu, index_stays, load_mimic, mixed_cohort
from codice_tesi.backend.mimic_comparison import compare_mimic, mapped_headers
from codice_tesi.backend.repository import read_packet, save_dataset
from codice_tesi.discharge.provenance import TraceResult, build_catalog
from codice_tesi.discharge.template import load_template


@pytest.fixture
def mimic_subset(tmp_path):
    def table(name, header, *records):
        with gzip.open(tmp_path / f"{name}.csv.gz", "wt", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(header.split())
            writer.writerows(records)

    start, end = "2141-01-01 12:00:00", "2141-01-02 12:00:00"
    table(
        "admissions",
        "subject_id hadm_id admittime dischtime admission_type "
        "admission_location discharge_location",
        ("10", "100", start, end, "URGENT", "ED", "HOME"),
        ("10", "101", start, end, "URGENT", "ED", "HOME"),
    )
    table("patients", "subject_id gender anchor_age anchor_year", ("10", "F", "40", "2141"))
    table(
        "discharge",
        "subject_id hadm_id note_id storetime charttime text",
        ("11", "100", "wrong-subject", end, end, "Altra persona"),
        ("10", "100", "reference", end, end, "Discharge Diagnosis:\nTesto riservato"),
    )
    table(
        "radiology",
        "subject_id hadm_id note_id charttime storetime text",
        ("10", "100", "valid", start, end, "Referto disponibile"),
        ("10", "100", "late", start, "2141-01-03 00:00:00", "Referto tardivo"),
        ("10", "101", "other", start, end, "Altro ricovero"),
    )
    for name in (
        "services",
        "transfers",
        "diagnoses_icd",
        "procedures_icd",
        "hcpcsevents",
        "drgcodes",
    ):
        table(name, "subject_id hadm_id")
    for name in ("d_icd_diagnoses", "d_icd_procedures"):
        table(name, "icd_code icd_version long_title")
    table("d_hcpcs", "code short_description")
    table("d_labitems", "itemid label fluid", ("42", "Esame A", "Blood"))
    table(
        "labevents",
        "subject_id hadm_id charttime storetime itemid value valuenum valueuom "
        "labevent_id specimen_id flag",
        *[
            ("10", "100", time, time, "42", value, "", "unità", value, "1", "")
            for time, value in (
                (start, "11"),
                ("2141-01-01 14:00:00", "22"),
                ("2141-01-01 15:00:00", "33"),
                (end, "44"),
                ("2141-01-02 13:00:00", "55"),
            )
        ],
    )
    table(
        "pharmacy",
        "subject_id hadm_id pharmacy_id proc_type status entertime verifiedtime "
        "frequency duration duration_interval",
        ("10", "100", "1", "Discharge Med", "Active", start, end, "QD", "5", "Days"),
        ("10", "100", "2", "Discharge Med", "Discontinued", start, end, "QD", "", ""),
        ("10", "100", "3", "Discharge Med", "Active", start, "2141-01-03 00:00:00", "QD", "", ""),
    )
    table(
        "prescriptions",
        "subject_id hadm_id pharmacy_id poe_id starttime drug dose_val_rx dose_unit_rx "
        "route doses_per_24_hrs",
        ("10", "100", "1", "a", "2141-01-03 00:00:00", "Farmaco A", "5", "mg", "PO", "1"),
        ("10", "100", "2", "b", start, "Farmaco revocato", "5", "mg", "PO", "1"),
        ("10", "100", "3", "c", start, "Farmaco tardivo", "5", "mg", "PO", "1"),
        ("10", "100", "4", "d", start, "Farmaco di reparto", "5", "mg", "PO", "1"),
    )
    table(
        "emar",
        "subject_id hadm_id emar_id pharmacy_id charttime storetime medication event_txt",
        ("10", "100", "event-a", "4", start, end, "Farmaco di reparto", "Not Given"),
    )
    table(
        "emar_detail",
        "subject_id emar_id administration_type dose_given dose_given_unit "
        "complete_dose_not_given route infusion_rate infusion_rate_unit",
        ("10", "event-a", "", "", "", "Yes", "PO", "", ""),
        ("11", "event-a", "Dettaglio di altro paziente", "", "", "", "", "", ""),
    )
    table(
        "microbiologyevents",
        "subject_id hadm_id charttime storetime chartdate storedate microevent_id "
        "micro_specimen_id spec_type_desc test_name org_name quantity ab_name interpretation",
        ("10", "100", start, end, "", "", "1", "a", "Blood", "Coltura A", "", "", "", ""),
    )
    table(
        "poe",
        "subject_id hadm_id poe_id ordertime order_type order_subtype transaction_type "
        "order_status discontinue_of_poe_id",
        ("10", "100", "order-a", start, "Procedure", "", "New", "Active", ""),
    )
    table("poe_detail", "subject_id poe_id field_name field_value", ("10", "order-a", "x", "y"))

    return load_mimic(tmp_path, tmp_path, limit=1, seed=42)


def test_mimic_joins_both_ids_and_keeps_target_out_of_evidence(mimic_subset):
    contexts, sources, references = mimic_subset
    assert [context.id for context in contexts] == ["mimic-100"]
    (reference,) = references["mimic-100"]
    assert reference["note_id"] == "reference"
    assert mapped_headers(reference["text"])["diagnosi_dimissione"] == ("Discharge Diagnosis",)
    assert any("Referto disponibile" in source.text for source in sources)
    assert all(
        text not in source.text
        for source in sources
        for text in ("Testo riservato", "Referto tardivo", "Altro ricovero", "Altra persona")
    )


def test_expanded_hosp_separates_timed_sources_and_discharge_orders(mimic_subset):
    _, sources, _ = mimic_subset
    by_origin = {source.origin: source.text for source in sources}
    labs = by_origin["hosp/labevents"]
    assert all(f"Esame A: {value} unità" in labs for value in (11, 33, 44))
    assert all(f"Esame A: {value} unità" not in labs for value in (22, 55))
    orders = by_origin["hosp/pharmacy + hosp/prescriptions (Discharge Med)"]
    assert "drug=Farmaco A" in orders and "frequency=QD" in orders
    assert "Farmaco di reparto" in by_origin["hosp/prescriptions"]
    assert not any(
        name in source.text
        for source in sources
        for name in ("Farmaco revocato", "Farmaco tardivo", "Dettaglio di altro paziente")
    )
    emar = by_origin["hosp/emar + hosp/emar_detail"]
    assert "event_txt=Not Given" in emar and "complete_dose_not_given=Yes" in emar
    assert "Coltura A" in by_origin["hosp/microbiologyevents"]
    assert "field_name=x | field_value=y" in by_origin["hosp/poe + hosp/poe_detail"]


def test_mixed_cohort_balances_paths_and_icu_rows_match_the_selected_stay(tmp_path):
    def table(name, header, records=()):
        with gzip.open(tmp_path / f"{name}.csv.gz", "wt", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(header.split())
            writer.writerows(records)

    start, end = "2141-01-01 10:00:00", "2141-01-02 10:00:00"
    keys = [(str(i), str(100 + i)) for i in range(4)]
    admissions = {key: {"admittime": start, "dischtime": end} for key in keys}
    table(
        "edstays",
        "subject_id hadm_id stay_id intime outtime",
        [(*key, "ed-" + key[0], start, end) for key in keys[:2]],
    )
    table(
        "icustays",
        "subject_id hadm_id stay_id intime outtime first_careunit last_careunit",
        [(*key, "icu-" + key[0], start, end, "MICU", "MICU") for key in keys[::2]],
    )
    table(
        "discharge",
        "subject_id hadm_id storetime charttime note_id text",
        [(*key, end, end, "note-" + key[0], "Riferimento") for key in keys],
    )
    table(
        "services",
        "subject_id hadm_id transfertime curr_service",
        [(*key, start, "service-" + key[0]) for key in keys],
    )
    table(
        "diagnoses_icd",
        "subject_id hadm_id seq_num icd_version icd_code",
        [(*key, "1", "10", "K0" + key[0]) for key in keys],
    )
    ed = index_stays(tmp_path, "edstays", admissions)
    icu = index_stays(tmp_path, "icustays", admissions)
    selected = mixed_cohort(tmp_path, tmp_path, admissions, ed, icu, limit=4, seed=42)
    assert set(selected) == set(keys)

    table("d_items", "itemid label", [("7", "Misura A")])
    table(
        "chartevents",
        "subject_id hadm_id stay_id charttime storetime itemid value valuenum valueuom",
        [
            (*keys[0], "icu-0", start, end, "7", "valido", "", ""),
            ("0", "101", "icu-0", start, end, "7", "ricovero diverso", "", ""),
            (*keys[0], "icu-0", start, "2141-01-03 10:00:00", "7", "tardivo", "", ""),
        ],
    )
    for name in (
        "datetimeevents",
        "outputevents",
        "inputevents",
        "ingredientevents",
        "procedureevents",
    ):
        table(name, "subject_id stay_id")
    groups = defaultdict(lambda: defaultdict(list))
    add_icu(tmp_path, icu, admissions, groups)
    [(time, source)] = groups[keys[0]]["icu/chartevents"]
    assert time == start and "value=valido" in source


def test_compare_mimic_requires_matching_version(empty_settings, mimic_subset, tmp_path):
    contexts, sources, references = mimic_subset
    context = contexts[0]
    template = load_template()
    draft_path = tmp_path / "draft.json"
    destination = tmp_path / "comparison.md"

    async def verify():
        async with Database(empty_settings) as database:
            await save_dataset(database, contexts, sources, references=references)
            packet = await read_packet(database, context_id=context.id)
            result = TraceResult.model_validate(
                {
                    "binding": {
                        "run_id": "test",
                        "context_id": context.id,
                        "dataset": {"id": context.dataset, "version": "stale"},
                        "template": {
                            "id": template.id,
                            "version": template.version,
                            "sha256": template.sha256,
                        },
                    },
                    "catalog": build_catalog(packet),
                    "draft": {
                        "sections": [
                            {"section_id": section.id, "text": "", "sources": ()}
                            for section in template.sections
                        ]
                    },
                    "validation": {"technical": "passed"},
                }
            )
            draft_path.write_text(result.model_dump_json())
            with pytest.raises(ValueError, match="versione"):
                await compare_mimic(database, draft_path, destination)
            assert not destination.exists()
            current = result.model_dump(mode="json")
            current["binding"]["dataset"]["version"] = context.dataset_version
            draft_path.write_text(json.dumps(current))
            await compare_mimic(database, draft_path, destination)
            assert "Testo riservato" in destination.read_text()
            assert "Testo riservato" not in packet.model_dump_json()

    asyncio.run(verify())
