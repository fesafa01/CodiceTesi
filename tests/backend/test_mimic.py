import asyncio
import csv
import gzip
import json
from collections import defaultdict

import pytest
from fastapi.testclient import TestClient

from codice_tesi.access import LocalIdentity
from codice_tesi.backend.app import create_app
from codice_tesi.backend.database import Database
from codice_tesi.backend.mimic import (
    add_icu,
    add_labs,
    index_stays,
    load_admissions,
    load_mimic,
    mixed_cohort,
)
from codice_tesi.backend.mimic_audit import audit_mimic
from codice_tesi.backend.mimic_comparison import compare_mimic, mapped_headers
from codice_tesi.backend.mimic_coverage import audit_candidate_coverage
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
        "frequency duration duration_interval medication route disp_sched",
        ("10", "100", "1", "Discharge Med", "Active", start, end, "QD", "5", "Days", "", "", "08"),
        ("10", "100", "2", "Discharge Med", "Discontinued", start, end, "QD", "", "", "", "", ""),
        (
            "10",
            "100",
            "3",
            "Discharge Med",
            "Active",
            start,
            "2141-01-03 00:00:00",
            "QD",
            "",
            "",
            "",
            "",
            "",
        ),
        (
            "10",
            "100",
            "5",
            "Discharge Med",
            "Active",
            start,
            end,
            "QD",
            "",
            "",
            "Farmaco solo in farmacia",
            "PO",
            "",
        ),
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
        ("10", "100", "order-b", start, "Procedure", "", "New", "Active", ""),
        ("10", "100", "order-c", end, "Procedure", "", "New", "Active", ""),
    )
    table(
        "poe_detail",
        "subject_id poe_id field_name field_value",
        ("10", "order-a", "x", "y"),
        ("10", "order-b", "instructions", "After discharge:\noutpatient follow-up in two weeks"),
        ("10", "order-c", "plan", "Post-discharge follow-up"),
        ("10", "order-c", "timing", "Two weeks"),
    )

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


def test_rich_pilot_import_labels_complete_case_and_keeps_reference_separate(
    tmp_path, mimic_subset
):
    def table(name, header, *records):
        with gzip.open(tmp_path / f"{name}.csv.gz", "wt", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(header.split())
            writer.writerows(records)

    start, end = "2141-01-01 12:00:00", "2141-01-02 12:00:00"
    table(
        "services",
        "subject_id hadm_id transfertime prev_service curr_service",
        ("10", "100", start, "", "MED"),
    )
    table(
        "diagnoses_icd",
        "subject_id hadm_id icd_version icd_code seq_num",
        ("10", "100", "10", "Z00", "1"),
    )
    table(
        "edstays",
        "subject_id hadm_id stay_id intime outtime arrival_transport disposition",
        ("10", "100", "ed-a", start, end, "WALK IN", "ADMITTED"),
    )
    table(
        "icustays",
        "subject_id hadm_id stay_id intime outtime first_careunit last_careunit",
        ("10", "100", "icu-a", start, end, "MICU", "MICU"),
    )
    table("triage", "subject_id stay_id chiefcomplaint")
    table("d_items", "itemid label")
    for name in (
        "vitalsign",
        "diagnosis",
        "medrecon",
        "pyxis",
        "chartevents",
        "datetimeevents",
        "outputevents",
        "inputevents",
        "ingredientevents",
        "procedureevents",
    ):
        table(name, "subject_id stay_id")

    contexts, sources, references = load_mimic(
        tmp_path,
        tmp_path,
        limit=1,
        seed=42,
        ed=tmp_path,
        icu=tmp_path,
        min_discharge_cases=1,
        min_complete_cases=1,
        rich_pilot=True,
    )
    (context,) = contexts
    assert context.display_name == "Caso completo"
    assert context.id == "mimic-100" and context.expanded_sources
    assert references[context.id][0]["note_id"] == "reference"
    assert any("Esame A: 22 unità" in source.text for source in sources)
    assert all("Testo riservato" not in source.text for source in sources)


def test_expanded_hosp_separates_timed_sources_and_discharge_orders(tmp_path, mimic_subset):
    _, sources, _ = mimic_subset
    by_origin = {source.origin: source.text for source in sources}
    labs = by_origin["hosp/labevents"]
    assert all(f"Esame A: {value} unità" in labs for value in (11, 33, 44))
    assert all(f"Esame A: {value} unità" not in labs for value in (22, 55))
    full = defaultdict(dict)
    admissions = load_admissions(tmp_path)
    add_labs(tmp_path, admissions, full, full=True)
    expanded = "\n".join(line for _, line in full["10", "100"]["labevents"])
    assert all(f"Esame A: {value} unità" in expanded for value in (11, 22, 33, 44))
    assert "Esame A: 55 unità" not in expanded
    orders = by_origin["hosp/pharmacy + hosp/prescriptions (Discharge Med)"]
    assert "drug=Farmaco A" in orders and "frequency=QD" in orders and "disp_sched=08" in orders
    standalone = next(line for line in orders.splitlines() if "pharmacy_id=5 |" in line)
    assert "drug=Farmaco solo in farmacia" in standalone and "route=PO" in standalone
    assert "dose_val_rx=" not in standalone
    followup = by_origin["hosp/poe + hosp/poe_detail (candidati follow-up)"]
    assert "poe_id=order-b" in followup and "outpatient follow-up in two weeks" in followup
    assert "poe_id=order-c" in followup and "field_value=Two weeks" in followup
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


def test_audit_mimic_reports_orders_and_details_omitted_from_old_package(
    tmp_path, empty_settings, mimic_subset, capsys
):
    contexts, sources, references = mimic_subset
    previous = []
    for source in sources:
        if source.origin.endswith("(candidati follow-up)"):
            continue
        if source.origin.endswith("(Discharge Med)"):
            source = source.model_copy(
                update={
                    "text": "\n".join(
                        line.replace(" | disp_sched=08", "")
                        for line in source.text.splitlines()
                        if "pharmacy_id=5 |" not in line
                    )
                }
            )
        if source.origin == "hosp/poe + hosp/poe_detail":
            source = source.model_copy(
                update={
                    "text": source.text + "\nOrdine POE | poe_id=order-c | "
                    "field_name=plan | field_value=Post-discharge follow-up"
                }
            )
        previous.append(source)

    async def verify():
        async with Database(empty_settings) as database:
            await save_dataset(database, contexts, previous, references=references)
            await audit_mimic(database, tmp_path, 1)

    asyncio.run(verify())
    output = capsys.readouterr().out
    assert "TOTALE\t4\t1\t1\t1\t2\t1\t1\t1" in output
    assert "ordine nominativo presente solo in pharmacy: 1" in output
    assert "terapia: non_attivo: 1" in output
    assert "terapia: fuori_finestra: 1" in output
    assert "poe: ordine assente nel pacchetto: 1" in output
    assert "poe: ordine presente, almeno un dettaglio o segnale assente: 1" in output
    assert all(value not in output for value in ("Farmaco A", "order-c", "mimic-100"))


def test_mixed_cohort_balances_paths_and_icu_rows_match_the_selected_stay(tmp_path, capsys):
    def table(name, header, records=()):
        with gzip.open(tmp_path / f"{name}.csv.gz", "wt", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(header.split())
            writer.writerows(records)

    start, end = "2141-01-01 10:00:00", "2141-01-02 10:00:00"
    keys = [(str(i), str(100 + i)) for i in range(5)]
    admissions = {key: {"admittime": start, "dischtime": end} for key in keys}
    table(
        "admissions",
        "subject_id hadm_id admittime dischtime admission_type "
        "admission_location discharge_location",
        [(*key, start, end, "URGENT", "ED", "HOME") for key in keys],
    )
    table(
        "edstays",
        "subject_id hadm_id stay_id intime outtime",
        [(*key, "ed-" + key[0], start, end) for key in keys[:2]],
    )
    table(
        "icustays",
        "subject_id hadm_id stay_id intime outtime first_careunit last_careunit",
        [(*key, "icu-" + key[0], start, end, "MICU", "MICU") for key in keys[:4:2]],
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
    assert set(keys[:3]) <= selected.keys() and len(selected) == 4
    (additional,) = set(keys) - selected.keys()
    pharmacy_header = (
        "subject_id hadm_id pharmacy_id proc_type status entertime verifiedtime medication"
    )
    pharmacy_rows = [
        (*additional, "1", "Discharge Med", "Active", start, end, "Farmaco A"),
        (*keys[1], "2", "Discharge Med", "Active", start, end, ""),
        (*keys[0], "3", "Discharge Med", "Discontinued", start, end, "Farmaco B"),
        (*keys[2], "4", "Discharge Med", "Active", start, "", "Farmaco C"),
    ]
    table("pharmacy", pharmacy_header, pharmacy_rows)
    table(
        "prescriptions",
        "subject_id hadm_id pharmacy_id drug",
        [(*keys[1], "2", "Farmaco D"), (*keys[2], "4", "Farmaco C")],
    )
    enriched = mixed_cohort(
        tmp_path, tmp_path, admissions, ed, icu, limit=4, seed=42, min_discharge_cases=2
    )
    assert set(enriched) == {*keys[:3], additional}
    with pytest.raises(ValueError, match="Solo 2 pazienti"):
        mixed_cohort(
            tmp_path, tmp_path, admissions, ed, icu, limit=4, seed=42, min_discharge_cases=3
        )

    table(
        "poe",
        "subject_id hadm_id poe_id ordertime order_type order_subtype "
        "transaction_type order_status",
        [
            (*keys[0], "followup-both", start, "Consults", "", "New", "Active"),
            (*additional, "followup-hosp", start, "Consults", "Follow-up", "New", "Active"),
            (*keys[1], "appointment-ed", start, "Consults", "", "New", "Active"),
            (*keys[0], "referral-inactive", start, "Consults", "Referral", "D/C", "Inactive"),
        ],
    )
    table(
        "poe_detail",
        "subject_id poe_id field_name field_value",
        [
            (keys[0][0], "followup-both", "plan", "Outpatient follow-up after discharge"),
            (keys[1][0], "appointment-ed", "plan", "Outpatient appointment"),
        ],
    )
    with pytest.raises(ValueError, match="Solo 0 pazienti soddisfano tutti i criteri"):
        mixed_cohort(
            tmp_path, tmp_path, admissions, ed, icu, limit=4, seed=42, min_complete_cases=1
        )
    table(
        "pharmacy",
        pharmacy_header,
        [*pharmacy_rows, (*keys[0], "5", "Discharge Med", "Active", start, end, "Farmaco E")],
    )
    enriched_both = mixed_cohort(
        tmp_path,
        tmp_path,
        admissions,
        ed,
        icu,
        limit=4,
        seed=42,
        min_discharge_cases=3,
        min_complete_cases=1,
    )
    assert set(enriched_both) == {*keys[:3], additional}

    table(
        "radiology",
        "subject_id hadm_id charttime storetime text",
        [
            (*keys[0], start, end, "Referto sintetico"),
            (*keys[1], start, "2141-01-03 10:00:00", "Referto tardivo"),
        ],
    )
    table("procedures_icd", "subject_id hadm_id icd_code chartdate", [(*keys[0], "p1", start[:10])])
    table("microbiologyevents", "subject_id hadm_id charttime storetime", [(*keys[0], start, end)])
    table(
        "triage",
        "subject_id stay_id chiefcomplaint",
        [(keys[0][0], "ed-" + keys[0][0], "Motivo sintetico")],
    )
    table(
        "labevents",
        "subject_id hadm_id value valuenum charttime storetime",
        [(*keys[0], "11", "", start, end)],
    )
    table(
        "emar",
        "subject_id hadm_id medication event_txt charttime storetime",
        [(*keys[0], "Farmaco A", "Not Given", start, end)],
    )
    audit_candidate_coverage(tmp_path, tmp_path, tmp_path, tmp_path, extended=True)
    output = capsys.readouterr().out
    assert "idonei\t1\t1\t1\t2" in output
    assert "terapia_poe_attuale\t1\t0\t0\t1" in output
    assert "terapia_poe_extra_senza_attuale\t0\t1\t0\t0" in output
    assert "extra_invio\t0\t0\t0\t0" in output
    assert "triage_radiologia_e_procedure\t1\t0\t0\t0" in output
    assert "laboratorio_radiologia_emar\t1\t0\t0\t0" in output
    assert "Outpatient appointment" not in output and "Referto sintetico" not in output
    rich = mixed_cohort(
        tmp_path,
        tmp_path,
        admissions,
        ed,
        icu,
        limit=4,
        seed=42,
        min_complete_cases=1,
        rich_pilot=True,
        ed=tmp_path,
    )
    assert set(rich) == {*keys[:3], additional}

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
            report = destination.read_text()
            assert "Testo riservato" in report
            assert "Specializzando e altro medico" not in report
            assert "In attesa di esame istologico" not in report
            assert "Terapia in dimissione" in report
            assert "Programma terapeutico, istruzioni e follow-up" in report
            assert "Testo riservato" not in packet.model_dump_json()

    asyncio.run(verify())


def test_reference_is_available_only_from_review_for_assigned_context_and_version(
    empty_settings, mimic_subset
):
    contexts, sources, references = mimic_subset
    context = contexts[0]
    reference = references[context.id][-1]
    references[context.id].insert(0, {**reference, "note_id": "older", "text": "Older version"})

    async def seed():
        async with Database(empty_settings) as database:
            await save_dataset(database, contexts, sources, references=references)

    asyncio.run(seed())
    identities = (
        LocalIdentity("reviewer", "assigned", frozenset({context.id})),
        LocalIdentity("other", "unassigned", frozenset()),
    )
    route = f"/api/v1/revisione/contesti/{context.id}/lettera-originale"
    packet = f"/api/v1/compilazione/contesti/{context.id}/pacchetto"
    version = {"versione": context.dataset_version}
    headers = {"Authorization": "Bearer assigned"}

    with TestClient(
        create_app(empty_settings, identities=identities, reference_only=True)
    ) as client:
        response = client.get(route, params=version, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"note_id": reference["note_id"], "text": reference["text"]}
        assert client.get(route, params=version).status_code == 401
        assert (
            client.get(
                route, params=version, headers={"Authorization": "Bearer unassigned"}
            ).status_code
            == 403
        )
        assert client.get(route, params={"versione": "0" * 64}, headers=headers).status_code == 404
        assert client.get(packet, headers=headers).status_code == 404

    with TestClient(create_app(empty_settings, identities=identities), headers=headers) as client:
        assert client.get(route, params=version).status_code == 404
        response = client.get(packet)
        assert response.status_code == 200 and reference["text"] not in response.text
