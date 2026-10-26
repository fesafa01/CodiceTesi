"""La selezione richiede fatti clinici disponibili e appartenenti al ricovero."""

import copy
from pathlib import Path
from runpy import run_path

import pytest


@pytest.fixture
def builder():
    return run_path(str(Path(__file__).parents[2] / "scripts/build_discharge_dataset.py"))


@pytest.fixture
def clinical_record():
    encounter = {
        "id": "e",
        "subject": {"reference": "Patient/p"},
        "period": {"start": "2023-06-20T07:00:00Z", "end": "2023-06-23T12:00:00Z"},
    }
    common = {"subject": {"reference": "Patient/p"}, "encounter": {"reference": "Encounter/e"}}
    resources = []
    for day in (20, 22):
        refs = []
        for number in range(3):
            oid = f"lab-{day}-{number}"
            refs.append({"reference": "Observation/" + oid})
            resources.append(
                {
                    "resourceType": "Observation",
                    "id": oid,
                    **common,
                    "status": "final",
                    "category": [{"coding": [{"code": "laboratory"}]}],
                    "code": {"coding": [{"system": "http://loinc.org", "code": str(number)}]},
                    "effectiveDateTime": f"2023-06-{day}T08:00:00Z",
                    "valueQuantity": {"value": number, "unit": "mg/dL"},
                }
            )
        resources.append(
            {
                "resourceType": "DiagnosticReport",
                "id": f"report-{day}",
                **common,
                "status": "final",
                "effectiveDateTime": f"2023-06-{day}T08:00:00Z",
                "issued": f"2023-06-{day}T10:00:00Z",
                "result": refs,
            }
        )
    resources.append(
        {
            "resourceType": "Procedure",
            "id": "procedure",
            **common,
            "status": "completed",
            "code": {"text": "Procedura documentata"},
            "performedDateTime": "2023-06-20T12:00:00Z",
        }
    )
    return encounter, resources


def test_serial_results_and_completed_procedure_support_a_dated_course(builder, clinical_record):
    encounter, resources = clinical_record
    metrics = builder["clinical_information"](resources, encounter)
    assert metrics["lab_results"] == 6  # Anche il valore zero è un risultato.
    assert metrics["distinct_lab_tests"] == 3 and metrics["serial_lab_tests"] == 3
    assert metrics["clinical_days"] == 2
    assert metrics["completed_procedures"] == 1
    metrics["discharge_orders_with_dosage"] = 1
    assert builder["information_gaps"](metrics) == []


@pytest.mark.parametrize("defect", ["late", "foreign", "wrong_patient", "empty", "synthetic"])
def test_unavailable_or_unusable_results_do_not_qualify_a_case(builder, clinical_record, defect):
    encounter, resources = clinical_record
    for r in resources:
        if r["resourceType"] != "Observation":
            continue
        if defect == "late":
            r["issued"] = "2023-06-24T08:00:00Z"
        elif defect == "foreign":
            r["encounter"] = {"reference": "Encounter/other"}
        elif defect == "wrong_patient":
            r["subject"] = {"reference": "Patient/other"}
        elif defect == "empty":
            r["valueQuantity"] = {"unit": "mg/dL"}
        else:
            r["meta"] = {
                "tag": [{"system": "urn:codice-tesi:synthetic-origin", "code": "scenario-decision"}]
            }
    metrics = builder["clinical_information"](resources, encounter)
    assert metrics["lab_results"] == 0
    assert {"distinct_lab_tests", "result_reports"} <= set(builder["information_gaps"](metrics))


def test_a_placeholder_note_or_historical_status_cannot_replace_a_procedure(
    builder, clinical_record
):
    encounter, resources = clinical_record
    resources[-1] = {
        "resourceType": "DiagnosticReport",
        "id": "placeholder",
        "status": "final",
        "subject": {"reference": "Patient/p"},
        "encounter": {"reference": "Encounter/e"},
        "code": {"text": "History and physical note"},
        "effectiveDateTime": "2023-06-20T07:00:00Z",
    }
    metrics = builder["clinical_information"](resources, encounter)
    assert metrics["result_reports"] == 2
    assert "completed_procedures" in builder["information_gaps"](metrics)


def test_cancelled_procedure_does_not_count_as_a_performed_treatment(builder, clinical_record):
    encounter, resources = clinical_record
    resources[-1]["status"] = "not-done"
    metrics = builder["clinical_information"](resources, encounter)
    assert metrics["completed_procedures"] == 0


def test_waiting_for_transplant_is_not_reused_as_final_postoperative_diagnosis(
    builder, clinical_record
):
    encounter, _ = clinical_record
    reason = {"code": "698306007", "display": "Awaiting transplantation of kidney"}
    assert builder["discharge_diagnosis"](reason, [], encounter) == (None, [])
    condition = {
        "resourceType": "Condition",
        "id": "post-transplant",
        "subject": {"reference": "Patient/p"},
        "encounter": {"reference": "Encounter/e"},
        "recordedDate": "2023-06-22T12:00:00Z",
        "code": {
            "coding": [
                {
                    "system": "http://snomed.info/sct",
                    "code": "161665007",
                    "display": "History of renal transplant",
                }
            ]
        },
    }
    original = copy.deepcopy(condition)
    concept, ids = builder["discharge_diagnosis"](reason, [condition], encounter)
    assert concept == condition["code"] and ids == [condition["id"]]
    assert condition == original
    condition["recordedDate"] = "2023-06-24T12:00:00Z"
    assert builder["discharge_diagnosis"](reason, [condition], encounter) == (None, [])
