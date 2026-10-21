"""Import FHIR: identità, confini temporali, dosaggi e sostituzione atomica."""

import asyncio
import base64

import pytest

from codice_tesi.backend.cli import main
from codice_tesi.backend.database import Database
from codice_tesi.backend.repository import BackendError, read_packet
from codice_tesi.backend.synthea import import_synthea, load_synthea


def event(typ, id, **fields):
    return {
        "resourceType": typ,
        "id": id,
        "subject": {"reference": "urn:uuid:p"},
        "encounter": {"reference": "urn:uuid:e"},
        **fields,
    }


def test_import_preserves_inpatient_values_doses_and_deduplicates_notes(fhir):
    text = "Anamnesi raccolta in degenza: nefropatia pregressa."
    note = {"contentType": "text/plain", "data": base64.b64encode(text.encode()).decode()}
    root = fhir(
        [
            event(
                "Observation",
                "lab",
                effectiveDateTime="2023-06-21T00:00:00Z",
                status="final",
                code={"text": "Test zero"},
                valueQuantity={"value": 0, "unit": "mg"},
            ),
            event(
                "MedicationRequest",
                "rx",
                authoredOn="2023-06-21T00:00:00Z",
                status="completed",
                medicationCodeableConcept={"text": "Farmaco"},
                category=[{"text": "Inpatient"}],
                dosageInstruction=[
                    {
                        "asNeededBoolean": True,
                        "doseAndRate": [{"doseQuantity": {"value": 2, "unit": "mg"}}],
                    }
                ],
            ),
            event(
                "Condition",
                "history",
                recordedDate="2020-01-01",
                code={"text": "Storia"},
                abatementDateTime="2024-01-01",
            ),
            event(
                "DocumentReference",
                "doc",
                date="2023-06-21T00:00:00Z",
                content=[{"attachment": note}],
            ),
            event(
                "DiagnosticReport",
                "report",
                effectiveDateTime="2023-06-21T00:00:00Z",
                presentedForm=[note],
            ),
        ]
    )
    digest, loaded = load_synthea(root)
    assert load_synthea(root) == (digest, loaded)
    context = loaded[0][0]
    assert (context.id, context.patient_id, context.episode_id, context.scope) == (
        "synthea-caso-01",
        "p",
        "e",
        "episodio",
    )
    sources = {s.kind: s for _, s in loaded}
    assert "Data effettiva di dimissione del ricovero selezionato: 2023-06-23T12:00:00Z" in (
        sources["contesto"].text
    )
    assert sources["nota-1"].text.endswith("\n\n" + text)
    assert "classe=IMP" in sources["nota-1"].text
    assert "doc" in sources["nota-1"].origin and "report" in sources["nota-1"].origin
    assert "0 mg" in sources["dati_ricovero"].text
    assert '"asNeededBoolean":true' in sources["dati_ricovero"].text
    assert '"value":2' in sources["dati_ricovero"].text
    assert "Inpatient" in sources["dati_ricovero"].text
    assert "anamnesi" not in sources
    assert "Storia" not in "\n".join(s.text for s in sources.values())


def test_other_encounters_future_results_and_refuted_conditions_are_excluded(fhir):
    root = fhir(
        [
            event("Observation", "future", effectiveDateTime="2024-01-01", code={"text": "FUTURO"}),
            event(
                "Observation",
                "late",
                effectiveDateTime="2023-06-21",
                issued="2023-07-01",
                code={"text": "TARDIVO"},
            ),
            event(
                "Observation",
                "other",
                effectiveDateTime="2023-06-21",
                encounter={"reference": "Encounter/other"},
                code={"text": "ALTRO"},
            ),
            event(
                "Condition",
                "wrong",
                recordedDate="2020-01-01",
                code={"text": "SMENTITA"},
                verificationStatus={"coding": [{"code": "refuted"}]},
            ),
        ]
    )
    _, loaded = load_synthea(root)
    assert len(loaded) == 1
    assert loaded[0][1].kind == "contesto"


def test_cross_patient_resource_and_changed_original_are_rejected(fhir):
    root = fhir(
        [
            event(
                "Observation",
                "wrong",
                effectiveDateTime="2023-06-21",
                subject={"reference": "Patient/other"},
            )
        ]
    )
    with pytest.raises(ValueError, match="altro paziente"):
        load_synthea(root)
    (root / "patient.json").write_text("{}")
    with pytest.raises(ValueError, match="Hash FHIR"):
        load_synthea(root)


def test_cli_replace_switches_active_dataset(empty_settings, fhir, monkeypatch):
    old = fhir(case_id="caso_02", patient_id="p2", encounter_id="e2")

    async def seed():
        async with Database(empty_settings) as db:
            await import_synthea(db, old)

    asyncio.run(seed())
    root = fhir()
    monkeypatch.setenv("CLINICAL_DATABASE_URL", empty_settings.database_url.get_secret_value())
    monkeypatch.setenv("CLINICAL_DATABASE_SCHEMA", empty_settings.database_schema)
    main(["import-synthea", str(root), "--replace"])

    async def verify():
        async with Database(empty_settings) as db:
            packet = await read_packet(db, seed=0)
            assert packet.context.id == "synthea-caso-01"
            with pytest.raises(BackendError):
                await read_packet(db, context_id="synthea-caso-02")

    asyncio.run(verify())
    # Un originale corrotto fallisce prima della sostituzione e lascia il DB intatto.
    (root / "patient.json").write_text("{}")
    with pytest.raises(SystemExit):
        main(["import-synthea", str(root), "--replace"])
    asyncio.run(verify())


def emergency(id="ed", **fields):
    return event(
        "Encounter",
        id,
        status="finished",
        **{
            "class": {"code": "EMER"},
            "period": {"start": "2023-06-20T06:00:00Z", "end": "2023-06-20T07:00:15Z"},
            **fields,
        },
    )


def test_ed_reference_does_not_expand_selected_stay(fhir):
    # Forma riscontrata in Delbert: PS sovrapposto di 15 s all'ingresso,
    # diagnosi nel PS referenziata dalle procedure del ricovero e context per i farmaci.
    note = {"contentType": "text/plain", "data": base64.b64encode(b"Nota PS").decode()}
    root = fhir(
        [
            emergency(),
            event(
                "Condition",
                "nstemi",
                encounter={"reference": "Encounter/ed"},
                recordedDate="2023-06-20T07:00:00Z",
                code={"coding": [{"system": "http://snomed.info/sct", "code": "401314000"}]},
            ),
            event(
                "Procedure",
                "inpatient",
                performedDateTime="2023-06-20T07:30:00Z",
                reasonReference=[{"reference": "Condition/nstemi"}],
            ),
            event(
                "Observation",
                "creatinine",
                encounter={"reference": "Encounter/ed"},
                effectiveDateTime="2023-06-20T06:30:00Z",
                issued="2023-06-20T08:00:00Z",
                code={"text": "Creatinine"},
                valueQuantity={"value": 2.8778, "unit": "mg/dL"},
            ),
            event(
                "MedicationAdministration",
                "given_ed",
                encounter={},
                context={"reference": "Encounter/ed"},
                effectiveDateTime="2023-06-20T06:30:00Z",
                status="completed",
                medicationCodeableConcept={"text": "Aspirin"},
            ),
            event(
                "MedicationAdministration",
                "given_inpatient",
                encounter={},
                context={"reference": "Encounter/e"},
                effectiveDateTime="2023-06-20T08:00:00Z",
                status="completed",
                medicationCodeableConcept={"text": "Ticagrelor"},
            ),
            event(
                "DiagnosticReport",
                "ed_report",
                encounter={"reference": "Encounter/ed"},
                effectiveDateTime="2023-06-20T06:30:00Z",
                presentedForm=[note],
            ),
            event(
                "DocumentReference",
                "ed_note",
                encounter={},
                context={"encounter": [{"reference": "Encounter/ed"}]},
                date="2023-06-20T06:30:00Z",
                content=[{"attachment": note}],
            ),
        ]
    )
    _, loaded = load_synthea(root)
    context = loaded[0][0]
    sources = {s.kind: s for _, s in loaded}
    assert context.episode_id == "e"
    assert set(sources) == {"contesto", "dati_ricovero"}
    text = "\n".join(s.text for s in sources.values())
    assert "2.8778 mg/dL" not in text and "Nota PS" not in text
    assert "MedicationAdministration | Aspirin" not in text
    assert "MedicationAdministration | Ticagrelor" in sources["dati_ricovero"].text
    assert "referenziate" not in text and "classe=EMER" not in text
    assert "anamnesi" not in sources


@pytest.mark.parametrize("inverse", [False, True])
def test_partof_link_does_not_expand_selected_stay_in_either_direction(fhir, inverse):
    ed_fields = {} if inverse else {"partOf": {"reference": "Encounter/e"}}
    updates = {"partOf": {"reference": "Encounter/ed"}} if inverse else {}
    root = fhir(
        [
            emergency(**ed_fields),
            event(
                "Observation",
                "ed_lab",
                encounter={"reference": "Encounter/ed"},
                effectiveDateTime="2023-06-20T06:30:00Z",
                code={"text": "PS"},
            ),
        ],
        main_updates=updates,
    )
    _, loaded = load_synthea(root)
    assert [s.kind for _, s in loaded] == ["contesto"]


def test_nearby_matching_emergency_encounter_is_not_imported(fhir):
    code = {"coding": [{"system": "snomed", "code": "123"}]}
    provider = {"reference": "Organization/hospital"}
    root = fhir(
        [
            emergency(reasonCode=[code], serviceProvider=provider),
            event(
                "Observation",
                "ed_lab",
                encounter={"reference": "Encounter/ed"},
                effectiveDateTime="2023-06-20T06:30:00Z",
                code={"text": "PS"},
            ),
        ],
        main_updates={"reasonCode": [code], "serviceProvider": provider},
    )
    _, loaded = load_synthea(root)
    assert [s.kind for _, s in loaded] == ["contesto"]


@pytest.mark.parametrize(
    "case",
    [
        "near_only",
        "provider",
        "code",
        "ambiguous",
        "other_patient",
        "remote",
        "future",
        "late_link",
    ],
)
def test_unrelated_ambiguous_future_or_cross_patient_accesses_are_excluded(fhir, case):
    code = {"coding": [{"system": "snomed", "code": "123"}]}
    provider = {"reference": "Organization/hospital"}
    fields = {"reasonCode": [code], "serviceProvider": provider}
    if case == "near_only":
        fields = {}
    if case == "provider":
        fields["serviceProvider"] = {"reference": "Organization/other"}
    if case == "code":
        fields["reasonCode"] = [{"coding": [{"system": "snomed", "code": "999"}]}]
    if case == "other_patient":
        fields["subject"] = {"reference": "Patient/other"}
    if case == "remote":
        fields["period"] = {"start": "2023-06-18T06:00:00Z", "end": "2023-06-18T07:00:00Z"}
    if case == "future":
        fields["period"] = {"start": "2023-06-24T06:00:00Z", "end": "2023-06-24T07:00:00Z"}
    extra = [
        emergency(**fields),
        event(
            "Observation",
            "excluded",
            encounter={"reference": "Encounter/ed"},
            effectiveDateTime="2023-06-20T06:30:00Z",
            code={"text": "DO_NOT_INCLUDE"},
        ),
    ]
    if case == "ambiguous":
        extra.append(emergency("ed2", **fields))
    if case == "late_link":
        extra[0].pop("reasonCode")
        extra.extend(
            [
                event(
                    "Condition",
                    "target",
                    encounter={"reference": "Encounter/ed"},
                    recordedDate="2023-06-20T07:00:00Z",
                ),
                event(
                    "DiagnosticReport",
                    "late_report",
                    effectiveDateTime="2023-06-21T07:00:00Z",
                    issued="2023-06-24T07:00:00Z",
                    basedOn=[{"reference": "Condition/target"}],
                ),
            ]
        )
    root = fhir(extra, main_updates={"reasonCode": [code], "serviceProvider": provider})
    _, loaded = load_synthea(root)
    assert not any(s.kind.startswith("dati_accesso-") for _, s in loaded)
    assert "DO_NOT_INCLUDE" not in "\n".join(s.text for _, s in loaded)


def test_related_encounter_does_not_import_late_results_or_events_outside_its_period(fhir):
    root = fhir(
        [
            emergency(partOf={"reference": "Encounter/e"}),
            event(
                "Observation",
                "early",
                encounter={"reference": "Encounter/ed"},
                effectiveDateTime="2023-06-19T06:30:00Z",
                code={"text": "TOO_EARLY"},
            ),
            event(
                "Observation",
                "late",
                encounter={"reference": "Encounter/ed"},
                effectiveDateTime="2023-06-20T06:30:00Z",
                issued="2023-06-24T06:30:00Z",
                code={"text": "LATE_RESULT"},
            ),
            event(
                "Observation",
                "after",
                encounter={"reference": "Encounter/ed"},
                effectiveDateTime="2023-06-24T06:30:00Z",
                code={"text": "FUTURE"},
            ),
        ]
    )
    _, loaded = load_synthea(root)
    assert not any(s.kind.startswith("dati_accesso-") for _, s in loaded)


def test_main_stay_includes_context_documents_and_excludes_early_events(fhir):
    text = "Anamnesi riferita all'ingresso: pregresso intervento."
    root = fhir(
        [
            event(
                "DocumentReference",
                "current_note",
                encounter={},
                context={"encounter": [{"reference": "Encounter/e"}]},
                date="2023-06-20T08:00:00Z",
                content=[
                    {
                        "attachment": {
                            "contentType": "text/plain",
                            "data": base64.b64encode(text.encode()).decode(),
                        }
                    }
                ],
            ),
            event(
                "Observation",
                "early",
                effectiveDateTime="2023-06-20T06:59:59Z",
                code={"text": "PRIMA_DELLA_DEGENZA"},
            ),
            event(
                "Condition",
                "old",
                recordedDate="2020-01-01",
                encounter={"reference": "Encounter/remote"},
                code={"text": "STORIA_CDR"},
            ),
        ],
        main_updates={"reasonCode": [{"text": "MOTIVO_DOCUMENTATO_DEL_RICOVERO"}]},
    )
    _, loaded = load_synthea(root)
    sources = {s.kind: s for _, s in loaded}
    assert set(sources) == {"contesto", "nota-1"}
    assert text in sources["nota-1"].text
    assert "MOTIVO_DOCUMENTATO_DEL_RICOVERO" in sources["contesto"].text
    assert "STORIA_CDR" not in "\n".join(s.text for s in sources.values())


def test_extraction_version_changes_dataset_version_without_modifying_originals(fhir, monkeypatch):
    from codice_tesi.backend import synthea

    root = fhir()
    before = (root / "patient.json").read_bytes()
    old_version, _ = load_synthea(root)
    monkeypatch.setattr(synthea, "EXTRACTION_VERSION", "new-extraction")
    new_version, _ = load_synthea(root)
    assert new_version != old_version
    assert (root / "patient.json").read_bytes() == before
