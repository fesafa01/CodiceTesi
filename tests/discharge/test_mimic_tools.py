"""Contratti MIMIC interamente inventati: nessun campione, DB o modello reale."""

import asyncio
import hashlib
import json
from collections import defaultdict

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI

from codice_tesi.backend import mimic
from codice_tesi.config import GEMMA_MODEL, MISTRAL_MODEL, QWEN_MODEL, ModelConfig
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.facts import FactInventory, extract_facts
from codice_tesi.discharge.generator import LLMDraftGenerator
from codice_tesi.discharge.models import MIMIC_DATASET
from codice_tesi.discharge.prompts import (
    MIMIC_PROMPT_VERSION,
    MIMIC_TOOL_PROMPT_VERSION,
    build_prompt,
)
from codice_tesi.discharge.provenance import build_catalog
from codice_tesi.discharge.tools import DocumentaryTools


def packet_for(packet_factory, records):
    packet = packet_factory("dati_ricovero")
    context = packet.context.model_copy(update={"dataset": MIMIC_DATASET})
    sources = tuple(
        packet.sources[0].model_copy(
            update={
                "id": f"source-{index}",
                "origin": origin,
                "text": text,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
        for index, (origin, text) in enumerate(records)
    )
    return type(packet)(context=context, sources=sources, template=packet.template)


@pytest.fixture
def synthetic_mimic(packet_factory):
    return packet_for(
        packet_factory,
        [
            (
                "hosp/labevents",
                "2140-01-01 08:00:00 | Esame inventato: 10 mg/L | itemid=42 | campione=Blood\n"
                "2140-01-01 16:00:00 | Esame inventato: 12 mg/L | itemid=42 | campione=Blood\n",
            ),
            (
                "hosp/prescriptions",
                "2140-01-01 09:00:00 | Ordine farmacologico in degenza | drug=Farmaco inventato | "
                "dose_val_rx=5 | dose_unit_rx=mg | route=PO | doses_per_24_hrs=2\n",
            ),
            (
                "hosp/emar + hosp/emar_detail",
                "2140-01-01 10:00:00 | Evento registrato eMAR | medication=Farmaco inventato | "
                "event_txt=Not Given | complete_dose_not_given=Yes\n",
            ),
            (
                "hosp/pharmacy + hosp/prescriptions (Discharge Med)",
                "2140-01-02 11:00:00 | Ordine di farmacia per dimissione | "
                "drug=Farmaco inventato | "
                "status=active | dose_val_rx=5 | dose_unit_rx=mg | route=PO | frequency=BID\n",
            ),
            (
                "ed/vitalsign",
                "Misure in pronto soccorso | stay_id=fixture | charttime=2140-01-01 07:00:00 | "
                "heartrate=90 | temperature=98.6 | pain=0\n",
            ),
            (
                "hosp/procedures_icd",
                "2140-01-01 | Procedura ICD codificata retrospettivamente | ICD-10: TEST | "
                "Procedura inventata | seq_num=1\n",
            ),
        ],
    )


def test_mimic_tools_return_real_synthetic_records_with_original_provenance(synthetic_mimic):
    catalog = build_catalog(synthetic_mimic)
    facts = extract_facts(catalog, dataset=MIMIC_DATASET)
    assert facts.extraction_version == "mimic-record-v1" and len(facts.facts) == 8
    tools = DocumentaryTools(catalog, facts)
    latest = tools.execute("riepiloga_osservazioni", esame="Esame inventato")
    item = latest["items"][0]
    assert item["ordering"] == "documented_local_time"
    assert item["documented_count"] == 2 and not item["latest_ambiguous"]
    assert item["events"][0]["attributes"]["value"] == "12"
    assert latest["coverage"]["recognized_facts"] == 8
    assert latest["coverage"]["empty_reason"] is None
    meds = tools.execute("ricostruisci_terapia_documentata", farmaco="Farmaco inventato")
    events = meds["items"][0]["events"]
    assert [e["record_role"] for e in events] == [
        "inpatient_order",
        "emar_event",
        "discharge_order",
    ]
    assert events[1]["status"] == "Not Given"
    assert events[0]["missing_structured_fields"] == []
    assert "dose" in events[1]["missing_structured_fields"]
    timeline = tools.execute("ricostruisci_sequenza_eventi", limit=10)
    assert len(timeline["items"]) == 8
    assert not tools.execute("ricostruisci_sequenza_eventi", tipo="Procedure")["items"]
    # Ogni output deriva dallo snapshot, non dal testo della lettera finale.
    evidences = {e.id: e for e in catalog.evidences}
    sources = {s.id: s for s in catalog.sources}
    for event in timeline["items"]:
        evidence = evidences[event["evidence_id"]]
        assert evidence.source_id == event["source_id"]
        assert event["label"] in sources[evidence.source_id].text[evidence.start : evidence.end]
    vital = tools.execute("riepiloga_osservazioni", esame="temperature")["items"][0]["events"][0]
    assert vital["attributes"]["value"] == "98.6" and vital["attributes"]["unit"] is None
    assert (
        tools.execute("riepiloga_osservazioni", esame="inesistente")["coverage"]["empty_reason"]
        == "no_matching_records"
    )


def test_adapter_output_is_consumed_without_changing_sources(monkeypatch, packet_factory):
    """Round trip dell'adattatore reale su righe inventate, senza file CSV."""
    at = "2140-01-01 08:00:00"
    row = defaultdict(
        str,
        subject_id="fixture",
        hadm_id="episode",
        charttime=at,
        storetime=at,
        itemid="42",
        value="7",
        valueuom="mg/L",
        labevent_id="1",
    )
    monkeypatch.setattr(
        mimic, "rows", lambda *a, **k: [{"itemid": "42", "label": "Esame inventato"}]
    )
    monkeypatch.setattr(mimic, "linked", lambda *a, **k: [(("fixture", "episode"), row)])
    admissions = {("fixture", "episode"): {"admittime": at, "dischtime": "2140-01-02 12:00:00"}}
    groups = defaultdict(lambda: defaultdict(list))
    mimic.add_labs(None, admissions, groups)
    text = groups["fixture", "episode"]["labevents"][0][1]
    packet = packet_for(packet_factory, [("hosp/labevents", text)])
    catalog = build_catalog(packet)
    facts = extract_facts(catalog, dataset=MIMIC_DATASET)
    result = DocumentaryTools(catalog, facts).execute("riepiloga_osservazioni")
    assert result["items"][0]["events"][0]["attributes"]["value"] == "7"
    assert catalog.sources[0].text == text


def test_medication_adapter_roundtrip_preserves_not_given(monkeypatch, packet_factory):
    at = "2140-01-01 08:00:00"
    key = ("synthetic", "episode")
    prescription = defaultdict(
        str,
        starttime=at,
        drug="Farmaco inventato",
        pharmacy_id="rx",
        dose_val_rx="5",
        dose_unit_rx="mg",
        route="PO",
    )
    emar = defaultdict(
        str,
        subject_id=key[0],
        hadm_id=key[1],
        emar_id="administration",
        charttime=at,
        storetime=at,
        medication="Farmaco inventato",
        event_txt="Not Given",
    )
    tables = {"prescriptions": [prescription], "emar": [emar]}
    monkeypatch.setattr(
        mimic,
        "linked",
        lambda root, table, admissions: [(key, row) for row in tables.get(table, [])],
    )
    monkeypatch.setattr(mimic, "rows", lambda *a, **k: [])
    groups = defaultdict(lambda: defaultdict(list))
    mimic.add_medications(
        None, {key: {"admittime": at, "dischtime": "2140-01-02 12:00:00"}}, groups
    )
    packet = packet_for(
        packet_factory,
        [("hosp/" + origin, entries[0][1]) for origin, entries in sorted(groups[key].items())],
    )
    catalog = build_catalog(packet)
    result = DocumentaryTools(catalog, extract_facts(catalog, dataset=MIMIC_DATASET)).execute(
        "ricostruisci_terapia_documentata"
    )
    events = result["items"][0]["events"]
    assert {event["record_role"] for event in events} == {"inpatient_order", "emar_event"}
    assert next(e for e in events if e["record_role"] == "emar_event")["status"] == "Not Given"


def test_ed_adapter_roundtrip_has_explicit_clock_and_no_inferred_units(monkeypatch, packet_factory):
    at = "2140-01-01 08:00:00"
    key = ("synthetic", "episode")
    stay = {
        "subject_id": key[0],
        "stay_id": "stay",
        "intime": at,
        "outtime": "2140-01-01 12:00:00",
        "arrival_transport": "",
        "disposition": "",
    }
    row = defaultdict(str, subject_id=key[0], stay_id="stay", charttime=at, heartrate="80")
    monkeypatch.setattr(
        mimic, "rows", lambda root, table, **kwargs: [row] if table == "vitalsign" else []
    )
    groups = defaultdict(lambda: defaultdict(list))
    mimic.add_ed(None, {(key[0], "stay"): (key, stay)}, {key: {}}, groups)
    packet = packet_for(packet_factory, [("ed/vitalsign", groups[key]["ed/vitalsign"][0][1])])
    catalog = build_catalog(packet)
    facts = extract_facts(catalog, dataset=MIMIC_DATASET)
    assert len(facts.facts) == 1
    assert facts.facts[0].label == "heartrate" and facts.facts[0].attributes["unit"] is None


def test_mimic_unknown_records_and_missing_or_invalid_times_are_not_inferred(packet_factory):
    packet = packet_for(
        packet_factory,
        [
            ("hosp/labevents", "2140-02-30 08:00:00 | Esame: 5 mg/L | itemid=42\n"),
            ("ed/vitalsign", "Misure in pronto soccorso | stay_id=fixture | heartrate=80\n"),
            ("ed/medrecon", "Terapia riferita all'ingresso | name=Farmaco inventato\n"),
            ("ed/pyxis", "Prelievo farmaco dal dispensatore | name=Farmaco inventato\n"),
            ("hosp/radiology", "2140-01-01 | Observation | Testo libero | final | 5 mg/L\n"),
        ],
    )
    catalog = build_catalog(packet)
    facts = extract_facts(catalog, dataset=MIMIC_DATASET)
    assert facts.facts == ()
    assert (
        DocumentaryTools(catalog, facts).execute("riepiloga_osservazioni")["coverage"][
            "empty_reason"
        ]
        == "inventory_empty"
    )


def test_mimic_repeated_doses_qualitative_results_and_icu_keep_original_meaning(packet_factory):
    packet = packet_for(
        packet_factory,
        [
            ("hosp/labevents", "2140-01-01 | Esame: Non rilevato | itemid=42\n"),
            (
                "hosp/emar + hosp/emar_detail",
                "2140-01-01 08:00:00 | Evento registrato eMAR | "
                "medication=Farmaco inventato | event_txt=Administered | dose_given=1 | "
                "dose_given=2 | dose_given_unit=mg\n",
            ),
            (
                "icu/procedureevents",
                "2140-01-01 08:30:00 | Evento procedurale ICU | "
                "Procedura inventata | statusdescription=FinishedRunning\n",
            ),
            (
                "icu/inputevents",
                "2140-01-01 08:30:00 | Evento di infusione ICU | "
                "Infusione inventata | amount=0 | amountuom=mL | rate=1 | rateuom=mL/hour\n",
            ),
            (
                "icu/chartevents",
                "2140-01-01 08:30:00 | Misura ICU | Misura inventata | value=4 | valueuom=unità\n",
            ),
        ],
    )
    catalog = build_catalog(packet)
    facts = extract_facts(catalog, dataset=MIMIC_DATASET)
    tools = DocumentaryTools(catalog, facts)
    obs = tools.execute("riepiloga_osservazioni", esame="Esame")["items"][0]["events"][0]
    assert obs["attributes"]["documented_value"] == "Non rilevato"
    assert "value" not in obs["attributes"]
    med = tools.execute("ricostruisci_terapia_documentata", farmaco="Farmaco inventato")["items"][
        0
    ]["events"][0]
    assert med["attributes"]["record_fields"]["dose_given"] == ["1", "2"]
    assert "dose" in med["missing_structured_fields"]
    assert len(tools.execute("ricostruisci_sequenza_eventi", tipo="Procedure")["items"]) == 1
    infusion = tools.execute("ricostruisci_terapia_documentata", farmaco="Infusione inventata")[
        "items"
    ][0]["events"][0]
    assert "dose" not in infusion["missing_structured_fields"]


@pytest.mark.parametrize("model", [QWEN_MODEL, GEMMA_MODEL, MISTRAL_MODEL])
@pytest.mark.parametrize("thinking", [False, True])
def test_tool_fix_preserves_requests_without_tools(synthetic_mimic, model, thinking):
    """L'inventario non entra nel prompt; i rilievi preesistenti non cambiano."""
    from codice_tesi.discharge.facts import _discharge_therapy_issues, _followup_issues

    catalog = build_catalog(synthetic_mimic)
    previous = FactInventory(
        issues=tuple(_discharge_therapy_issues(catalog) + _followup_issues(catalog))
    )
    expected = build_prompt(synthetic_mimic, catalog, previous)
    prepared = prepare_request(
        synthetic_mimic,
        ContextConfig(),
        output_tokens=32768,
        model_config=ModelConfig(model=model, thinking=thinking),
    )
    assert prepared.prompt == expected
    assert prepared.prompt_version == MIMIC_PROMPT_VERSION
    assert "# Verifiche documentali abilitate" not in prepared.instructions
    assert prepared.facts.facts


@pytest.mark.parametrize("model", [QWEN_MODEL, GEMMA_MODEL, MISTRAL_MODEL])
def test_nonempty_mimic_tool_result_reaches_model_and_changes_final_response(
    synthetic_mimic, model
):
    """Il trasporto OpenAI è simulato: nessuna richiesta esterna, nessun campione."""
    config = ModelConfig(model=model, tools_enabled=True)
    prepared = prepare_request(
        synthetic_mimic, ContextConfig(), output_tokens=32768, model_config=config
    )
    assert prepared.prompt_version == MIMIC_TOOL_PROMPT_VERSION
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "synthetic-call",
                        "type": "function",
                        "function": {
                            "name": "riepiloga_osservazioni",
                            "arguments": json.dumps({"esame": "Esame inventato"}),
                        },
                    }
                ]
            }
            reason = "tool_calls"
        else:
            returned = [m for m in body["messages"] if m["role"] == "tool"]
            assert len(returned) == 1 and returned[0]["tool_call_id"] == "synthetic-call"
            result = json.loads(returned[0]["content"])
            event = result["items"][0]["events"][0]
            # L'output simulato dipende dal valore effettivamente ricevuto dal tool.
            delta = {"content": json.dumps({"value_from_tool": event["attributes"]["value"]})}
            reason = "stop"
        frame = {
            "id": "synthetic-response",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n",
        )

    async def run():
        async with AsyncOpenAI(
            base_url="http://synthetic.invalid/v1",
            api_key="synthetic",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            generator = LLMDraftGenerator(config, client)
            output = "".join([chunk async for chunk in generator.stream(prepared)])
            assert json.loads(output) == {"value_from_tool": "12"}
            assert len(generator.tool_results) == 1
            summary = generator.usage["metrics"]["tool_summary"]
            assert summary["recognized_facts"] == 8
            assert summary["nonempty"] == summary["completed"] == summary["requested"] == 1
            assert summary["empty"] == summary["arguments_rejected"] == 0
            assert "inventato" not in json.dumps(summary)

    asyncio.run(run())
    assert len(calls) == 2
