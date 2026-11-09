import hashlib

from codice_tesi.backend.clinical_packing import LEVELS, grouped_records, lab_records, pack_evidence
from codice_tesi.discharge.models import EvidenceSource


def source(identifier, origin, text):
    return EvidenceSource(
        id=identifier,
        context_id="mimic-1",
        kind="dati_ricovero",
        text=text,
        origin=origin,
        version="full",
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def test_lab_packing_preserves_original_first_last_min_and_max():
    records = [
        f"2141-01-{i + 1:02} 12:00:00 | Esame: {value} mg | itemid=42 | flag=abnormal"
        for i, value in enumerate([5, 3, 999, -2, 8, 9, 10])
    ]
    assert lab_records(records, 1) == [records[i] for i in (0, 2, 3, 6)]
    assert all(line in records for line in lab_records(records, 1))


def test_dose_and_status_changes_are_distinct_and_whole_records_are_kept():
    records = [
        "2141-01-01 | dose_val_rx=5 | drug=A | route=PO",
        "2141-01-02 | dose_val_rx=5 | drug=A | route=PO",
        "2141-01-03 | dose_val_rx=10 | drug=A | route=PO",
        "2141-01-04 | dose_val_rx=10 | drug=A | route=PO",
    ]
    assert grouped_records(records, ("drug", "dose_val_rx", "route"), 4) == records
    packed = grouped_records(records, ("drug", "dose_val_rx", "route"), 2)
    assert packed == [records[1], records[3]]


def test_final_medications_followup_codes_and_large_single_records_are_not_cut():
    final = source("a", "hosp/pharmacy + hosp/prescriptions (Discharge Med)", "x" * 100000)
    followup = source(
        "b", "hosp/poe + hosp/poe_detail (candidati follow-up)", "Controllo esplicito"
    )
    codes = source("c", "hosp/diagnoses_icd", "Codici originali")
    evidence, audit = pack_evidence([final, followup, codes], LEVELS[-1])
    assert evidence == [final, followup, codes]
    assert not audit["changes"]


def test_radiology_is_selected_by_whole_report_and_omissions_are_audited():
    radiology = [
        source(
            str(i),
            f"note/radiology/report-{i}",
            f"charttime=2141-01-0{i + 1} 12:00:00\nReferto originale {i}\n" + "A" * 1000,
        )
        for i in range(5)
    ]
    evidence, audit = pack_evidence(radiology, LEVELS[0])
    assert evidence == [radiology[i] for i in (0, 3, 4)]
    assert len(audit["changes"]) == 2
    assert all(not change["included"] for change in audit["changes"])


def test_packing_preserves_source_hash_integrity_and_original_rows():
    records = [
        f"2141-01-{i + 1:02} | medication=A | pharmacy_id=1 | event_txt=Administered"
        for i in range(20)
    ]
    original = source("e", "hosp/emar + hosp/emar_detail", "MIMIC-IV emar\n" + "\n".join(records))
    packed, audit = pack_evidence([original], LEVELS[-1])
    assert packed[0].text.splitlines()[1:] == [records[0], records[-1]]
    assert EvidenceSource.model_validate(packed[0].model_dump()) == packed[0]
    assert audit["changes"][0]["original_records"] == 20
    assert audit["changes"][0]["selected_records"] == 2
    assert audit["changes"][0]["original_sha256"] == original.sha256
