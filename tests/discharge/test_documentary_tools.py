"""Elaborazioni verificabili: ambiguità temporali, provenienza e limiti espliciti."""

import ast
import json
from pathlib import Path

import pytest

from codice_tesi.discharge.documentary import DocumentaryAnalysis
from codice_tesi.discharge.facts import extract_facts
from codice_tesi.discharge.provenance import build_catalog
from codice_tesi.discharge.tools import DocumentaryTools


def tools_for(packet_factory, text, **kwargs):
    catalog = build_catalog(packet_factory("dati_ricovero", text=text))
    return DocumentaryTools(catalog, extract_facts(catalog), **kwargs)


def test_latest_observation_compares_instants_not_offset_strings(packet_factory):
    tools = tools_for(
        packet_factory,
        "2026-11-01T12:00:00+02:00 | Observation | Sodio | final | 139 mmol/L\n"
        "2026-11-01T10:30:00Z | Observation | Sodio | preliminary | 140 mmol/L\n"
        "2026-11-01T11:30:00+01:00 | Observation | Sodio | final | 141 mmol/L\n"
        "2026-11-01T09:00:00Z | Observation | Sodio | final | 130 mmol/L\n",
    )
    item = tools.execute("riepiloga_osservazioni")["items"][0]
    assert item["documented_count"] == 4 and item["latest_ambiguous"]
    assert item["ordering"] == "instant"
    assert [f["attributes"]["value"] for f in item["events"]] == ["140", "141"]
    assert [f["status"] for f in item["events"]] == ["preliminary", "final"]
    assert len(tools.consulted) == 2


def test_missing_clock_or_timezone_keeps_all_candidates_of_latest_date(packet_factory):
    tools = tools_for(
        packet_factory,
        "2026-10-31 | Observation | Esame | final | 1 mg/L\n"
        "2026-11-01 | Observation | Esame | final | 2 mg/L\n"
        "2026-11-01T23:00:00Z | Observation | Esame | final | 3 mmol/L\n",
    )
    item = tools.execute("riepiloga_osservazioni", esame="Esame")["items"][0]
    assert item["ordering"] == "documented_date_only" and item["latest_ambiguous"]
    assert [f["attributes"]["unit"] for f in item["events"]] == ["mg/L", "mmol/L"]
    history = tools.execute("riepiloga_osservazioni", esame="Esame", storico=True)
    assert len(history["items"][0]["events"]) == 3
    assert tools.execute("riepiloga_osservazioni", esame="esame")["items"] == []


def test_medication_sequence_keeps_dosage_roles_and_documented_gaps(packet_factory):
    dosage = [{"text": "Una dose secondo nota", "doseAndRate": [{"doseQuantity": {"value": 0}}]}]
    tools = tools_for(
        packet_factory,
        "2026-11-02 | MedicationAdministration | Prodotto A | stato nello snapshot=completed\n"
        "2026-11-01 | MedicationRequest | Prodotto A | stato nello snapshot=active | "
        f"dosageInstruction={json.dumps(dosage)}\n"
        "2026-11-03 | MedicationRequest | Altro nome | stato nello snapshot=active\n",
    )
    result = tools.execute("ricostruisci_terapia_documentata", farmaco="Prodotto A")
    events = result["items"][0]["events"]
    assert [f["record_role"] for f in events] == ["prescription", "administration"]
    assert events[0]["attributes"]["dosageInstruction"] == dosage
    assert events[0]["missing_structured_fields"] == ["regimen_timing", "route"]
    assert events[0]["issues"][0]["code"] == "dose_unit_missing"
    assert not events[1]["dosage_documented"]
    assert events[1]["missing_structured_fields"] == ["dose", "regimen_timing", "route"]
    assert result["matches_total"] == 1
    assert "non" in result["items"][0]["notice"]
    assert "Prodotto A" not in json.dumps(tools.audit())
    assert "Una dose" not in json.dumps(tools.audit())


def test_chronology_orders_full_timestamps_and_keeps_source_links(packet_factory):
    tools = tools_for(
        packet_factory,
        "2026-11-01T12:00:00+02:00 | Procedure | Prima | stato nello snapshot=completed\n"
        "2026-11-01T09:30:00Z | Procedure | Prima ancora | stato nello snapshot=completed\n"
        "2026-11-02T09:00:00Z | Observation | Fuori | final | 2 mmol/L\n",
    )
    result = tools.execute(
        "ricostruisci_sequenza_eventi", tipo="Procedure", dal="2026-11-01", al="2026-11-01"
    )
    assert [f["label"] for f in result["items"]] == ["Prima ancora", "Prima"]
    ids = [f["evidence_id"] for f in result["items"]]
    originals = tools.execute("leggi_evidenze", ids=ids)
    assert all(f["source_id"] == "source-0" for f in originals["items"])
    assert "Prima ancora" in originals["items"][0]["text"]


@pytest.mark.parametrize(
    "args,code",
    [
        ({"dal": "20261101"}, "invalid_date"),
        ({"al": "2026-02-30"}, "invalid_date"),
        ({"dal": "2026-11-02", "al": "2026-11-01"}, "inverted_dates"),
        ({"tipo": "NON_SUPPORTATO"}, "invalid_type"),
        ({"offset": -1}, "invalid_limit"),
    ],
)
def test_invalid_filters_have_safe_codes_and_audit(packet_factory, args, code):
    tools = tools_for(packet_factory, "nota senza record")
    with pytest.raises(ValueError) as error:
        tools.execute("ricostruisci_sequenza_eventi", **args)
    assert error.value.code == code
    assert tools.audit()["calls"][0]["error_code"] == code
    assert tools.results == []


def test_pagination_by_bytes_is_contiguous_and_never_truncates_groups(packet_factory):
    text = "".join(f"2026-11-01 | Observation | Esame {i} | final | {i} mmol/L\n" for i in range(4))
    tools = tools_for(packet_factory, text)
    first = tools.execute("riepiloga_osservazioni", limit=1)
    tools.max_result_bytes = tools._size(first) + 5
    second = tools.execute("riepiloga_osservazioni", limit=10)
    assert len(second["items"]) == 1 and second["next_offset"] == 1
    assert second["limited"] and second["matches_total"] == 4
    assert tools._size(second) <= tools.max_result_bytes
    last = tools.execute("riepiloga_osservazioni", offset=3)
    assert last["items"][0]["label"] == "Esame 3"
    assert not last["limited"] and last["next_offset"] is None
    tools.max_result_bytes = 200
    with pytest.raises(ValueError, match="budget"):
        tools.execute("riepiloga_osservazioni")


def test_no_structured_data_is_explicitly_partial_not_clinical_absence(packet_factory):
    tools = tools_for(packet_factory, "Nessuna interpretazione automatica di questa nota.")
    result = tools.execute("ricostruisci_terapia_documentata")
    assert result["items"] == [] and result["matches_total"] == 0
    assert result["coverage"]["recognized_facts"] == 0
    assert result["coverage"]["passages_not_interpreted"] == 1
    assert "non dimostrano assenza" in result["coverage"]["notice"]


def test_foreign_inventory_is_rejected_before_any_tool_execution(packet_factory):
    catalog = build_catalog(packet_factory("nota"))
    foreign = extract_facts(
        build_catalog(
            packet_factory(
                "dati_ricovero", text="2026-11-01 | Observation | Esame | final | 1 mmol/L\n"
            )
        )
    )
    fact = foreign.facts[0].model_copy(update={"source_id": "fonte-estranea"})
    with pytest.raises(ValueError, match="catalogo"):
        DocumentaryTools(catalog, foreign.model_copy(update={"facts": (fact,)}))


def test_domain_analysis_has_no_framework_transport_or_observability_dependencies():
    import codice_tesi.discharge.documentary as module

    tree = ast.parse(Path(module.__file__).read_text())
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(
        s.startswith(("pydantic_ai", "openai", "httpx", "opentelemetry"))
        or ".observability" in s
        or ".backend" in s
        or ".web_api" in s
        for s in imports
        if s
    )
    assert DocumentaryAnalysis.__module__ == module.__name__


def test_original_dosage_json_cannot_inject_audit_references(packet_factory):
    dosage = [
        {
            "text": "Fonte dichiarata nel testo",
            "evidence_id": "estranea",
            "evidence_ids": ["altra-estranea"],
        }
    ]
    tools = tools_for(
        packet_factory,
        f"2026-11-01 | MedicationRequest | Prodotto | dosageInstruction={json.dumps(dosage)}\n",
    )
    result = tools.execute("ricostruisci_terapia_documentata")
    assert result["items"][0]["events"][0]["attributes"]["dosageInstruction"] == dosage
    assert tools.consulted == {"evidenza-0001"}
    assert "estranea" not in json.dumps(tools.audit())


def test_fields_from_separate_dosage_instructions_do_not_hide_gaps(packet_factory):
    dosage = [
        {"doseAndRate": [{"doseQuantity": {"value": 1, "unit": "mg"}}]},
        {"route": {"text": "orale"}, "timing": {"repeat": {"frequency": 2}}},
    ]
    tools = tools_for(
        packet_factory,
        f"2026-11-01 | MedicationRequest | Prodotto | dosageInstruction={json.dumps(dosage)}\n",
    )
    event = tools.execute("ricostruisci_terapia_documentata")["items"][0]["events"][0]
    assert event["missing_structured_fields"] == ["dose", "regimen_timing", "route"]
