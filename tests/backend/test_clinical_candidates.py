"""Screening sintetico e aggiunta atomica senza alterare il campione precedente."""

import argparse
import asyncio
import csv
import gzip
import hashlib
import json

import pytest
from psycopg.errors import UniqueViolation

from codice_tesi.backend.clinical_candidates import (
    PREFIX,
    access_update,
    add_historical_omr,
    append_candidates,
    campus_coverage,
    clinical_quotas,
    existing_contexts,
    full_pool,
    run_clinical_candidates,
    screen_candidates,
    select_shortlist,
)
from codice_tesi.backend.database import Database
from codice_tesi.backend.mimic import prepare_mimic, selected_references
from codice_tesi.backend.repository import read_packet, save_dataset
from codice_tesi.discharge.models import MIMIC_DATASET, Context, EvidenceSource


@pytest.fixture
def clinical_csvs(tmp_path):
    tables = {}

    def table(name, header, records=()):
        tables[name] = header.split(), list(records)

    start, end = "2141-01-01 12:00:00", "2141-01-03 12:00:00"
    subjects = list(range(1, 13)) + [100]
    table(
        "admissions",
        "subject_id hadm_id admittime dischtime admission_type "
        "admission_location discharge_location hospital_expire_flag deathtime",
        [(str(i), str(i * 10), start, end, "URGENT", "ED", "HOME", "0", "") for i in subjects],
    )
    table(
        "patients",
        "subject_id gender anchor_age anchor_year",
        [(str(i), "F", "40", "2141") for i in subjects],
    )
    table(
        "discharge",
        "subject_id hadm_id note_id storetime charttime text",
        [
            (
                str(i),
                str(i * 10),
                f"reference-{i}",
                end,
                end,
                "RIFERIMENTO ESCLUSIVO con informazioni da non usare nello screening",
            )
            for i in subjects
        ],
    )
    table(
        "radiology",
        "subject_id hadm_id note_id charttime storetime text",
        [(str(i), str(i * 10), f"rr-{i}", start, start, "Referto disponibile.") for i in subjects],
    )
    table(
        "services",
        "subject_id hadm_id transfertime prev_service curr_service",
        [
            (str(i), str(i * 10), start, "", "MED" if i <= 8 or i == 100 else "SURG")
            for i in subjects
        ],
    )
    table(
        "transfers",
        "subject_id hadm_id transfer_id eventtype careunit intime outtime",
        [(str(i), str(i * 10), str(i), "admit", "Medicine", start, end) for i in subjects],
    )
    table(
        "diagnoses_icd",
        "subject_id hadm_id seq_num icd_code icd_version",
        [(str(i), str(i * 10), "1", "486", "9") for i in subjects],
    )
    table(
        "procedures_icd",
        "subject_id hadm_id seq_num chartdate icd_code icd_version",
        [(str(i), str(i * 10), "1", "2141-01-02", "123", "9") for i in subjects if i >= 9],
    )
    table("d_icd_diagnoses", "icd_code icd_version long_title", [("486", "9", "Diagnosi")])
    table("d_icd_procedures", "icd_code icd_version long_title", [("123", "9", "Procedura")])
    table("d_hcpcs", "code short_description")
    table("hcpcsevents", "subject_id hadm_id")
    table("drgcodes", "subject_id hadm_id")
    table("d_labitems", "itemid label fluid", [("42", "Esame", "Blood")])
    table(
        "labevents",
        "subject_id hadm_id charttime storetime itemid value valuenum valueuom "
        "labevent_id specimen_id flag ref_range_lower ref_range_upper priority comments",
        [
            (
                str(i),
                str(i * 10),
                time,
                time,
                "42",
                value,
                value,
                "unità",
                f"{i}-{j}",
                "1",
                "",
                "1",
                "10",
                "STAT",
                "Commento utile",
            )
            for i in subjects
            for j, (time, value) in enumerate(
                [
                    (start, "1"),
                    ("2141-01-01 13:00:00", "999"),
                    ("2141-01-02 12:00:00", "3"),
                    (end, "4"),
                ]
            )
        ],
    )
    table(
        "pharmacy",
        "subject_id hadm_id pharmacy_id poe_id proc_type status entertime "
        "verifiedtime frequency duration duration_interval medication route disp_sched "
        "starttime stoptime",
        [
            (
                str(i),
                str(i * 10),
                str(i),
                f"poe-{i}",
                "Discharge Med",
                "Active",
                start,
                end,
                "QD",
                "5",
                "Days",
                "Farmaco finale",
                "PO",
                "08",
                end,
                "2141-01-08 12:00:00",
            )
            for i in subjects
        ],
    )
    rx_header = (
        "subject_id hadm_id pharmacy_id poe_id starttime stoptime drug_type drug "
        "dose_val_rx dose_unit_rx route doses_per_24_hrs prod_strength form_rx gsn ndc"
    )
    table(
        "prescriptions",
        rx_header,
        [
            (
                str(i),
                str(i * 10),
                str(i),
                f"poe-{i}",
                end,
                "2141-01-08 12:00:00",
                "MAIN",
                "Farmaco finale",
                "5",
                "mg",
                "PO",
                "1",
                "5 mg Tablet",
                "TABLET",
                "123",
                "456",
            )
            for i in subjects
        ]
        + [
            (
                str(i),
                str(i * 10),
                f"ward-{i}",
                f"poe-ward-{i}",
                start,
                end,
                "MAIN",
                "Vancomycin",
                "1",
                "g",
                "IV",
                "2",
                "1g Vial",
                "VIAL",
                "789",
                "012",
            )
            for i in range(5, 9)
        ],
    )
    table(
        "emar",
        "subject_id hadm_id emar_id pharmacy_id charttime storetime medication "
        "event_txt poe_id scheduletime",
        [
            (
                str(i),
                str(i * 10),
                f"emar-{i}",
                str(i),
                start,
                start,
                "Farmaco",
                "Administered",
                f"poe-{i}",
                start,
            )
            for i in subjects
        ],
    )
    table(
        "emar_detail",
        "subject_id emar_id administration_type dose_given dose_given_unit "
        "complete_dose_not_given route infusion_rate infusion_rate_unit parent_field_ordinal "
        "dose_due dose_due_unit product_description",
        [
            (
                str(i),
                f"emar-{i}",
                "IV Bolus",
                "5",
                "mg",
                "No",
                "IV",
                "",
                "",
                "1.1",
                "5",
                "mg",
                "Prodotto",
            )
            for i in subjects
        ],
    )
    table(
        "microbiologyevents",
        "subject_id hadm_id charttime storetime chartdate storedate "
        "microevent_id micro_specimen_id spec_type_desc test_name org_name quantity ab_name "
        "interpretation test_seq isolate_num dilution_text dilution_comparison "
        "dilution_value comments",
        [
            (
                str(i),
                str(i * 10),
                start,
                end,
                "2141-01-01",
                "2141-01-03",
                str(i),
                str(i),
                "Blood",
                "Coltura",
                "Organismo",
                "",
                "Vancomycin",
                "S",
                "1",
                "1",
                "<=1",
                "<=",
                "1",
                "Commento microbiologia",
            )
            for i in range(5, 9)
        ],
    )
    table(
        "poe",
        "subject_id hadm_id poe_id ordertime order_type order_subtype transaction_type "
        "order_status discontinue_of_poe_id discontinued_by_poe_id",
        [
            (str(i), str(i * 10), f"poe-{i}", start, "General Care", "", "New", "Active", "", "")
            for i in subjects
        ],
    )
    table("poe_detail", "subject_id poe_id field_name field_value")
    ed_subjects = list(range(1, 9)) + [100]
    table(
        "edstays",
        "subject_id hadm_id stay_id intime outtime arrival_transport disposition",
        [
            (
                str(i),
                str(i * 10),
                str(i),
                "2141-01-01 10:00:00",
                "2141-01-01 13:00:00",
                "WALK IN",
                "ADMITTED",
            )
            for i in ed_subjects
        ],
    )
    table(
        "triage",
        "subject_id stay_id chiefcomplaint acuity temperature heartrate resprate "
        "o2sat sbp dbp pain",
        [
            (str(i), str(i), "Sintomo", "3", "98", "80", "15", "98", "120", "80", "0")
            for i in ed_subjects
        ],
    )
    table("vitalsign", "subject_id stay_id")
    table("diagnosis", "subject_id stay_id")
    table(
        "medrecon",
        "subject_id stay_id charttime name gsn ndc etc_rn etccode etcdescription",
        [
            (str(i), str(i), start, "Farmaco abituale", "1", "2", "1", "3", "Classe")
            for i in ed_subjects
        ],
    )
    table("pyxis", "subject_id stay_id charttime name med_rn gsn_rn gsn")
    table(
        "omr",
        "subject_id chartdate seq_num result_name result_value",
        [(str(i), "2140-12-20", "1", "Weight", "150") for i in subjects]
        + [
            ("1", "2141-01-01", "1", "Weight", "MISURA GIORNO INGRESSO ESCLUSA"),
            ("1", "2141-01-04", "1", "Weight", "MISURA FUTURA ESCLUSA"),
        ],
    )
    for name, (header, records) in tables.items():
        with gzip.open(tmp_path / f"{name}.csv.gz", "wt", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(header)
            writer.writerows(records)
    return tmp_path


def baseline(count=1):
    context = Context(
        id="mimic-9000",
        dataset=MIMIC_DATASET,
        dataset_version="previous",
        scope="episodio",
        patient_id="100",
        episode_id="9000",
        display_name="Caso Hosp 1",
    )
    source = EvidenceSource(
        id="old-source",
        context_id=context.id,
        kind="dati_ricovero",
        text="Fonte precedente",
        origin="hosp/labevents",
        version="previous",
        sha256=hashlib.sha256(b"Fonte precedente").hexdigest(),
    )
    contexts, sources, refs = [], [], {}
    for number in range(count):
        identifier = f"mimic-{9000 + number}"
        contexts.append(
            context.model_copy(
                update={
                    "id": identifier,
                    "patient_id": str(100 + number),
                    "episode_id": str(9000 + number),
                    "display_name": f"Caso Hosp {number + 1}",
                }
            )
        )
        sources.append(
            source.model_copy(
                update={
                    "id": "old-source" if number == 0 else f"old-source-{number}",
                    "context_id": identifier,
                }
            )
        )
        refs[identifier] = [{"note_id": f"old-ref-{number}", "text": "Riferimento precedente"}]
    return contexts, sources, refs


def prepared(root):
    old, _, _ = baseline()
    existing = {c.id: c.model_dump(mode="json") for c in old}
    admissions, stays, metadata, counts = screen_candidates(
        root, root, root, existing, seed=42, pool_per_profile=10
    )
    paired = selected_references(root, admissions)
    contexts, sources, references = prepare_mimic(
        root,
        root,
        admissions,
        paired,
        seed=42,
        ed=root,
        ed_stays=stays,
        rich_pilot=True,
        clinical_details=True,
    )
    add_historical_omr(root, admissions, contexts, sources)
    return contexts, sources, references, metadata, counts


def test_screen_excludes_existing_patients_and_keeps_three_profiles(clinical_csvs):
    contexts, sources, refs, metadata, counts = prepared(clinical_csvs)
    assert len(contexts) == 12
    assert all(c.patient_id != "100" for c in contexts)
    assert set(counts.values()) == {4}
    assert len(refs) == 12
    assert not any("RIFERIMENTO ESCLUSIVO" in s.text for s in sources)
    joined = "\n".join(s.text for s in sources)
    assert "Esame: 999 unità" in joined  # Il picco intermedio rimane disponibile.
    assert all(
        value in joined
        for value in (
            "prod_strength=5 mg Tablet",
            "stoptime=2141-01-08",
            "dose_due=5",
            "dilution_value=1",
            "etcdescription=Classe",
            "priority=STAT",
        )
    )
    assert "MISURA GIORNO INGRESSO" not in joined and "MISURA FUTURA" not in joined
    assert "result_name=Weight" in joined
    assert len(metadata) == 12


def test_shortlist_quotas_names_coverage_and_recovered_columns(clinical_csvs):
    contexts, sources, refs, metadata, _ = prepared(clinical_csvs)
    selected, evidence, references, reports, rejected = select_shortlist(
        contexts, sources, refs, metadata, limit=10, minimum=0, maximum=80000, seed=42
    )
    assert len(selected) == len(references) == 10 and not rejected
    assert [c.display_name for c in selected] == [f"{PREFIX}{i:02}" for i in range(1, 11)]
    assert len({c.patient_id for c in selected}) == 10
    assert [r["profile"] for r in reports].count("medico_acuto_ed") == 4
    assert [r["profile"] for r in reports].count("infettivologico_ed") == 3
    assert [r["profile"] for r in reports].count("procedurale_hosp") == 3
    assert len({c.dataset_version for c in selected} | {s.version for s in evidence}) == 1
    for report in reports:
        assert len(report["campus"]) == 17
        assert report["campus"]["condizioni_dimissione"]["status"] == "parziale"
        assert report["campus"]["programma_followup"]["status"] == "non_documentato"
        assert report["campus"]["istologico_in_attesa"]["status"] == "da_verificare"
        assert report["recovered_columns"]["prescriptions"]["prod_strength"] >= 0
        assert report["recovered_columns"]["discharge_med"]["prod_strength"] == 1
    empty, _ = campus_coverage([])
    assert empty["diagnosi_codificate"]["status"] == "non_documentato"


def test_floor_and_ceiling_reject_without_relaxing_quotas(clinical_csvs, monkeypatch):
    contexts, sources, refs, metadata, _ = prepared(clinical_csvs)
    monkeypatch.setattr(
        "codice_tesi.backend.clinical_candidates.input_estimates",
        lambda *a: {"a": {"estimate": 100}, "b": {"estimate": 200}},
    )
    for minimum, maximum, reason in [(101, 80000, "sotto_floor"), (0, 199, "sopra_ceiling")]:
        audit = []
        with pytest.raises(ValueError, match=reason):
            select_shortlist(
                contexts,
                sources,
                refs,
                metadata,
                limit=10,
                minimum=minimum,
                maximum=maximum,
                seed=42,
                audit=audit,
            )
        assert len(audit) == 12
        assert all(row["exclusion"] == reason for row in audit)
    assert clinical_quotas(10) == {
        "medico_acuto_ed": 4,
        "infettivologico_ed": 3,
        "procedurale_hosp": 3,
    }


def test_adaptive_selection_fits_budget_and_reports_full_and_selected_sources(
    clinical_csvs,
    monkeypatch,
):
    contexts, sources, refs, metadata, _ = prepared(clinical_csvs)
    original = next(
        s for s in sources if s.context_id == "mimic-10" and s.origin == "hosp/labevents"
    )
    lines = [
        f"2141-01-01 12:0{i}:00 | Esame: {value} unità | itemid={item} | labevent_id={item}-{i}"
        for item in range(28)
        for i, value in enumerate((5, 100, -2, 10))
    ]
    text = "MIMIC-IV labevents\n" + "\n".join(lines)
    expanded = original.model_copy(
        update={"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    )
    sources = [expanded if s.id == original.id else s for s in sources]

    def estimates(context, evidence):
        count = sum(len(s.text.splitlines()) - 1 for s in evidence if s.origin == "hosp/labevents")
        return {"qwen": {"estimate": count * 500}, "prudenziale": {"estimate": count * 1000}}

    monkeypatch.setattr("codice_tesi.backend.clinical_candidates.input_estimates", estimates)
    _, evidence, _, reports, _ = select_shortlist(
        contexts,
        sources,
        refs,
        metadata,
        limit=10,
        minimum=0,
        maximum=80000,
        seed=42,
        source_profile="clinical",
    )
    report = next(r for r in reports if r["context_id"] == "mimic-10")
    assert report["packing"]["level"] == "compatto"
    assert [a["level"] for a in report["packing_attempts"]] == ["bilanciato", "compatto"]
    lab = next(s for s in evidence if s.id == original.id)
    assert len(lab.text.splitlines()) == 65
    assert all(line in lines for line in lab.text.splitlines()[1:])
    assert report["full_metrics"]["lab_items_with_repeated_measurements"] == 28
    assert report["lab_items_with_repeated_measurements"] == 16


def rewrite_csv(root, table, change):
    path = root / f"{table}.csv.gz"
    with gzip.open(path, "rt", newline="") as file:
        reader = csv.DictReader(file)
        header, records = reader.fieldnames, list(reader)
    records = change(records)
    with gzip.open(path, "wt", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=header)
        writer.writeheader()
        writer.writerows(records)


def test_screen_excludes_deaths_icu_and_late_microbiology(clinical_csvs):
    def death(records):
        for row in records:
            if row["subject_id"] == "1":
                row["hospital_expire_flag"] = "1"
        return records

    def icu(records):
        for row in records:
            if row["subject_id"] == "2":
                row["careunit"] = "Medical Intensive Care Unit (MICU)"
        return records

    def late(records):
        for row in records:
            row["storetime"] = "2141-01-04 00:00:00"
        return records

    rewrite_csv(clinical_csvs, "admissions", death)
    rewrite_csv(clinical_csvs, "transfers", icu)
    rewrite_csv(clinical_csvs, "microbiologyevents", late)
    old, _, _ = baseline()
    admissions, _, metadata, counts = screen_candidates(
        clinical_csvs,
        clinical_csvs,
        clinical_csvs,
        {c.id: c.model_dump(mode="json") for c in old},
        seed=42,
        pool_per_profile=10,
    )
    assert all(key[0] not in {"1", "2", "100"} for key in admissions)
    assert counts["infettivologico_ed"] == 0
    assert not any(value["screening_positive_culture"] for value in metadata.values())


def test_poe_inverse_link_does_not_reveal_future_order(clinical_csvs):
    def modify(records):
        records[0]["discontinued_by_poe_id"] = "late-order"
        records[1]["discontinued_by_poe_id"] = "current-order"
        for index, name, time in [
            (0, "late-order", "2141-01-04 00:00:00"),
            (1, "current-order", "2141-01-02 00:00:00"),
        ]:
            records.append(
                {**records[index], "poe_id": name, "ordertime": time, "discontinued_by_poe_id": ""}
            )
        return records

    rewrite_csv(clinical_csvs, "poe", modify)
    _, sources, _, _, _ = prepared(clinical_csvs)
    text = "\n".join(s.text for s in sources)
    assert "late-order" not in text
    assert "discontinued_by_poe_id=current-order" in text


def test_access_preserves_token_and_previous_assignments(tmp_path):
    path = tmp_path / "access.env"
    token = "x" * 43
    path.write_text(f"# Locale\nAPP_MIMIC_TOKEN={token}\nAPP_MIMIC_CONTEXTS=mimic-9000\n")
    access_update(path, ["mimic-10"], write=True)
    text = path.read_text()
    assert f"APP_MIMIC_TOKEN={token}" in text
    assert "mimic-10,mimic-9000" in text and "# Locale" in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_addition_preserves_previous_packets_and_resumes_without_rescan(
    clinical_csvs,
    empty_settings,
    tmp_path,
    monkeypatch,
):
    args = argparse.Namespace(
        hosp=clinical_csvs,
        notes=clinical_csvs,
        ed=clinical_csvs,
        limit=10,
        seed=42,
        min_input_tokens=0,
        max_input_tokens=80000,
        pool_per_profile=10,
        expect_existing=30,
        report_dir=tmp_path / "report",
        access_file=tmp_path / "access.env",
    )
    old_ids = ",".join(f"mimic-{9000 + i}" for i in range(30))
    args.access_file.write_text(f"APP_MIMIC_TOKEN={'x' * 43}\nAPP_MIMIC_CONTEXTS={old_ids}\n")

    async def verify():
        async with Database(empty_settings) as db:
            old, old_sources, old_refs = baseline(30)
            await save_dataset(db, old, old_sources, references=old_refs)
            before = {c.id: await read_packet(db, context_id=c.id) for c in old}
            args.prepare_only = True
            access_before = args.access_file.read_bytes()
            await run_clinical_candidates(db, args)
            assert len(await existing_contexts(db)) == 30
            assert args.access_file.read_bytes() == access_before
            assert (
                args.report_dir / "pool-completo-privato.json.gz"
            ).stat().st_mode & 0o777 == 0o600
            args.prepare_only = False
            await run_clinical_candidates(db, args)
            assert {c.id: await read_packet(db, context_id=c.id) for c in old} == before
            assert len(await existing_contexts(db)) == 40
            report = json.loads((args.report_dir / "candidati.json").read_text())
            assert len(report["candidates"]) == 10 and report["status"] == "aggiunto"
            with (args.report_dir / "copertura-campus.csv").open() as file:
                assert len(list(csv.reader(file))) == 171
            monkeypatch.setattr(
                "codice_tesi.backend.clinical_candidates.screen_candidates",
                lambda *a, **kw: pytest.fail("Riavvio: nessuna nuova scansione"),
            )
            await run_clinical_candidates(db, args)
            assert len(await existing_contexts(db)) == 40
            assert (
                len(args.access_file.read_text().split("APP_MIMIC_CONTEXTS=")[1].strip().split(","))
                == 40
            )
            async with db.connection(read_only=True) as connection:
                cursor = await connection.execute(
                    "SELECT context_id, notes FROM mimic_discharge_references "
                    "WHERE context_id = ANY(%s)",
                    (list(before),),
                )
                assert {r["context_id"]: r["notes"] for r in await cursor.fetchall()} == old_refs

    asyncio.run(verify())


def test_full_pool_is_cached_independently_of_token_budget(clinical_csvs, tmp_path, monkeypatch):
    old, _, _ = baseline()
    args = argparse.Namespace(
        hosp=clinical_csvs,
        notes=clinical_csvs,
        ed=clinical_csvs,
        seed=42,
        pool_per_profile=10,
        report_dir=tmp_path / "cache",
    )
    args.report_dir.mkdir()
    existing = {c.id: c.model_dump(mode="json") for c in old}
    before = full_pool(args, existing)
    monkeypatch.setattr(
        "codice_tesi.backend.clinical_candidates.prepare_mimic",
        lambda *a, **kw: pytest.fail("Il pool completo è già salvato"),
    )
    assert full_pool(args, existing) == before


def test_append_refuses_existing_patient_and_rolls_back_on_source_collision(
    clinical_csvs,
    empty_settings,
):
    contexts, sources, refs, metadata, _ = prepared(clinical_csvs)
    selected, evidence, references, _, _ = select_shortlist(
        contexts, sources, refs, metadata, limit=3, minimum=0, maximum=80000, seed=42
    )

    async def verify():
        async with Database(empty_settings) as db:
            old, old_sources, old_refs = baseline()
            await save_dataset(db, old, old_sources, references=old_refs)
            snapshot = await existing_contexts(db)
            reused = selected[0].model_copy(update={"patient_id": "100"})
            with pytest.raises(ValueError, match="paziente già presente"):
                await append_candidates(
                    db, [reused, *selected[1:]], evidence, references, expected=snapshot
                )
            bad = [evidence[0].model_copy(update={"id": "old-source"}), *evidence[1:]]
            with pytest.raises(UniqueViolation):
                await append_candidates(db, selected, bad, references, expected=snapshot)
            assert await existing_contexts(db) == snapshot
            with pytest.raises(ValueError, match="cambiato durante"):
                await append_candidates(db, selected, evidence, references, expected={})

    asyncio.run(verify())
