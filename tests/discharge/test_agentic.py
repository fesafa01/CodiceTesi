"""Passaggi originali, fatti conservativi, tool isolati e protocollo a più richieste."""

import asyncio
import json

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from codice_tesi import api, application
from codice_tesi.config import ModelConfig
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.facts import extract_facts
from codice_tesi.discharge.generator import LLMDraftGenerator
from codice_tesi.discharge.limits import (
    ExecutionTimeoutError,
    FinalPhaseToolCallError,
    ToolCallValidationError,
)
from codice_tesi.discharge.provenance import EvidenceCatalog, build_catalog
from codice_tesi.discharge.tools import DocumentaryTools
from codice_tesi.observability.telemetry import Telemetry, TelemetryConfig


@pytest.fixture
def traced_run(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry(TelemetryConfig(enabled=False, _env_file=None))
    telemetry.tracer = provider.get_tracer("test-tools")
    monkeypatch.setattr(application, "Telemetry", lambda _: telemetry)
    yield telemetry, exporter
    provider.shutdown()


def test_event_segmentation_preserves_unicode_negation_and_all_original_characters(packet_factory):
    text = (
        "Encounter episodio verificato\n"
        "2026-11-01 | Observation | Dolore 👁 | final | Non presente\n"
        "2026-11-02 | Observation | Sodio | final | 139 mmol/L\n"
    )
    catalog = build_catalog(packet_factory("dati_ricovero", text=text))
    passages = [e for e in catalog.evidences if e.source_id == "source-0"]
    assert len(passages) == 3
    assert "".join(text[e.start : e.end] for e in passages) == text
    assert "Non presente" in text[passages[1].start : passages[1].end]
    assert "139 mmol/L" not in text[passages[1].start : passages[1].end]
    assert passages[1].end != len(text[: passages[1].end].encode())
    assert build_catalog(packet_factory("dati_ricovero", text=text)) == catalog


def test_long_note_segments_keep_paragraphs_and_short_notes_remain_whole(packet_factory):
    text = "Non riferisce dolore. " * 100 + "\n\n" + "Controllo documentato. " * 100
    catalog = build_catalog(packet_factory("nota", text=text))
    passages = [e for e in catalog.evidences if e.source_id == "source-0"]
    assert len(passages) == 2
    assert "".join(text[e.start : e.end] for e in passages) == text
    assert len(build_catalog(packet_factory("nota")).evidences) == 2  # Nota e contesto.


def test_facts_preserve_values_units_dosage_and_signal_documentary_conflicts(packet_factory):
    dosage = [
        {
            "text": "Non modificare | dato originale",
            "doseAndRate": [{"doseQuantity": {"value": 1.0}}],
        }
    ]
    text = (
        "Encounter episodio\n"
        "2026-11-01 | Observation | Sodio | final | 139 mmol/L\n"
        "2026-11-01 | Observation | Sodio | final | 140 mmol/L\n"
        "2026-11-01 | MedicationRequest | Prodotto 25 MG | stato nello snapshot=active | "
        f"dosageInstruction={json.dumps(dosage)}\n"
    )
    catalog = build_catalog(packet_factory("dati_ricovero", text=text))
    inventory = extract_facts(catalog)
    assert len(inventory.facts) == 3
    assert inventory.facts[0].attributes["value"] == "139"
    assert inventory.facts[0].attributes["unit"] == "mmol/L"
    assert inventory.facts[-1].attributes["dosageInstruction"] == dosage
    assert {i.code for i in inventory.issues} == {"observation_disagreement", "dose_unit_missing"}
    assert all(i.evidence_ids for i in inventory.issues)
    assert extract_facts(build_catalog(packet_factory("nota", text=text))).facts == ()


def test_inventory_does_not_infer_units_or_turn_missing_into_zero(packet_factory):
    text = "2026-11-01 | Observation | Sodio | final | Dato assente: unknown\n"
    fact = extract_facts(build_catalog(packet_factory("dati_ricovero", text=text))).facts[0]
    assert "value" not in fact.attributes and "unit" not in fact.attributes


def test_reader_is_scoped_bounded_and_audits_errors_without_content(packet_factory):
    catalog = build_catalog(packet_factory("nota", text="Non riferisce dolore 👁."))
    reader = DocumentaryTools(catalog, extract_facts(catalog), max_calls=3)
    found = reader.execute("leggi_evidenze", ids=["evidenza-0001"])
    assert found["items"][0]["text"] == catalog.sources[0].text
    with pytest.raises(ValueError, match="snapshot"):
        reader.execute("leggi_evidenze", ids=["altra-degenza"])
    reader.execute("leggi_evidenze", ids=["evidenza-0001"])
    with pytest.raises(ValueError, match="tentativi"):
        reader.execute("leggi_evidenze", ids=["evidenza-0001"])
    assert reader.consulted == {"evidenza-0001"}
    assert reader.calls[1]["status"] == "failed"
    assert "dolore" not in json.dumps(reader.audit())


def test_reader_never_truncates_evidence_silently(packet_factory):
    catalog = build_catalog(packet_factory("nota", text="Non riferisce dolore. " * 100))
    reader = DocumentaryTools(catalog, extract_facts(catalog), max_result_bytes=1024)
    with pytest.raises(ValueError, match="budget"):
        reader.execute("leggi_evidenze", ids=["evidenza-0001"])


def test_failed_tool_is_traced_without_arguments_results_or_exception_text(
    packet_factory, traced_run
):
    telemetry, exporter = traced_run
    catalog = build_catalog(packet_factory("nota", text="PRIVATE_SOURCE_TEXT"))
    reader = DocumentaryTools(catalog, extract_facts(catalog), telemetry=telemetry)
    with telemetry.span("parent"):
        with pytest.raises(ValueError):
            reader.execute("riepiloga_osservazioni", esame="PRIVATE_QUERY", limit=0)
    spans = exporter.get_finished_spans()
    tool = next(s for s in spans if s.attributes.get("openinference.span.kind") == "TOOL")
    parent = next(s for s in spans if s.name == "parent")
    assert tool.parent.span_id == parent.context.span_id
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.attributes["tesi.tool.status"] == "failed"
    assert tool.attributes["error.type"] == "EvidenceReadError"
    assert tool.attributes["tesi.tool.failure_code"] == "invalid_limit"
    assert not tool.events
    dumped = tool.to_json()
    for secret in ("PRIVATE_SOURCE_TEXT", "PRIVATE_QUERY", "Il limite dei risultati"):
        assert secret not in dumped


def test_chronology_filters_dates_and_type_and_reports_limits(packet_factory):
    text = (
        "2026-11-02 | Observation | Sodio | final | 139 mmol/L\n"
        "2026-11-01 | Procedure | Esame | stato nello snapshot=completed\n"
        "2026-11-01 | Observation | Potassio | final | 4 mmol/L\n"
    )
    catalog = build_catalog(packet_factory("dati_ricovero", text=text))
    reader = DocumentaryTools(catalog, extract_facts(catalog))
    result = reader.execute(
        "ricostruisci_sequenza_eventi",
        tipo="Observation",
        dal="2026-11-01",
        al="2026-11-02",
        limit=1,
    )
    assert result["matches_total"] == 2 and result["limited"]
    assert result["items"][0]["label"] == "Potassio"
    with pytest.raises(ValueError):
        reader.execute("ricostruisci_sequenza_eventi", dal="non-data")


def test_shared_long_source_origin_is_returned_once_without_losing_passages(packet_factory):
    packet = packet_factory(
        "dati_ricovero",
        text="\n".join(
            [
                "2026-11-01 | Observation | Esame uno | final | 1 mmol/L",
                "2026-11-02 | Observation | Esame due | final | 2 mmol/L",
                "2026-11-03 | Observation | Esame tre | final | 3 mmol/L",
            ]
        ),
    )
    origin = "episodi/caso.json#" + "a" * 17000
    source = packet.sources[0].model_copy(update={"origin": origin})
    catalog = build_catalog(packet.model_copy(update={"sources": (source,)}))
    reader = DocumentaryTools(catalog, extract_facts(catalog))
    ids = [e.id for e in catalog.evidences if e.source_id == source.id]
    result = reader.execute("leggi_evidenze", ids=ids)
    assert len(result["items"]) == 3 and not result["limited"]
    assert result["source_metadata"][source.id]["origin"] == origin
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= reader.max_result_bytes
    assert all("origin" not in item for item in result["items"])
    evidences = {e.id: e for e in catalog.evidences}
    for item in result["items"]:
        evidence = evidences[item["evidence_id"]]
        assert item["text"] == source.text[evidence.start : evidence.end]


def chunk(delta, finish=None, usage=None):
    data = {
        "id": "mock",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        data["usage"] = usage
    return f"data: {json.dumps(data)}\n\n".encode()


@pytest.mark.parametrize("max_requests", [1, 4])
def test_real_client_tool_roundtrip_omits_narration_and_aggregates_usage(
    packet_factory,
    letter_factory,
    response_factory,
    monkeypatch,
    tmp_path,
    max_requests,
    traced_run,
):
    packet = packet_factory("nota", text="Non riferisce dolore.")
    final = response_factory(letter_factory())
    calls = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, first):
            self.first = first

        async def __aiter__(self):
            if self.first:
                yield chunk({"content": "Consulto il passaggio originale."})
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "lettura-1",
                                "type": "function",
                                "function": {
                                    "name": "leggi_evidenze",
                                    "arguments": json.dumps({"ids": ["evidenza-0001"]}),
                                },
                            }
                        ]
                    },
                    "tool_calls",
                    {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                )
            else:
                yield chunk(
                    {"content": final},
                    "stop",
                    {"prompt_tokens": 170, "completion_tokens": 30, "total_tokens": 200},
                )
            yield b"data: [DONE]\n\n"

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert {t["function"]["name"] for t in body.get("tools", [])} == {
            "leggi_evidenze",
            "riepiloga_osservazioni",
            "ricostruisci_terapia_documentata",
            "ricostruisci_sequenza_eventi",
        }
        if len(calls) == 2:
            returns = [m for m in body["messages"] if m["role"] == "tool"]
            assert len(returns) == 1 and "Non riferisce dolore." in returns[0]["content"]
            assert returns[0]["tool_call_id"] == "lettura-1"
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=Stream(len(calls) == 1)
        )

    def client(*args, **kwargs):
        return AsyncOpenAI(
            base_url="http://test/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(api, "create_client", client)
    monkeypatch.setenv("TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("HARDWARE_ENABLED", "false")
    run = application.run_application(
        backend_url="http://test",
        output_dir=tmp_path,
        packet=packet,
        console=False,
        config=ModelConfig(tools_enabled=True, max_tokens=8192, max_requests=max_requests),
    )
    if max_requests == 1:
        with pytest.raises(FinalPhaseToolCallError):
            asyncio.run(run)
        assert len(calls) == 1 and not list(tmp_path.rglob("bozza.txt"))
        report = json.loads(next(tmp_path.rglob("esito.json")).read_text())
        assert report["richieste_llm_avviate"] == 1
        assert report["consumo"]["metrics"]["usage_complete"]
        assert report["stato"] == "fallito"
        assert report["consumo"]["tools"]["consulted_evidence_ids"] == []
        assert all(c["error_code"] == "final_phase" for c in report["consumo"]["tools"]["calls"])
        return
    path = asyncio.run(run)
    _, exporter = traced_run
    spans = exporter.get_finished_spans()
    tool_spans = [s for s in spans if s.attributes.get("openinference.span.kind") == "TOOL"]
    assert len(tool_spans) == 1
    tool = tool_spans[0]
    llm = next(s for s in spans if s.attributes.get("openinference.span.kind") == "LLM")
    assert tool.context.trace_id == llm.context.trace_id
    agent = next(s for s in spans if s.name == "discharge.agent_run")
    requests = sorted(
        (s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"),
        key=lambda s: s.attributes["tesi.request.index"],
    )
    assert len(requests) == 2
    assert tool.parent.span_id == agent.context.span_id
    assert all(s.parent.span_id == agent.context.span_id for s in requests)
    assert tool.attributes["tesi.tool.request_index"] == 1
    assert requests[0].end_time <= tool.start_time <= tool.end_time <= requests[1].start_time
    assert [s.attributes["llm.token_count.prompt"] for s in requests] == [100, 170]
    assert [s.attributes["llm.token_count.completion"] for s in requests] == [20, 30]
    assert requests[0].attributes["gen_ai.response.finish_reasons"] == ("tool_calls",)
    assert requests[0].attributes["tesi.request.tool_calls_requested"] == 1
    assert requests[0].attributes["tesi.request.timing.first_tool_call_seconds"] >= 0
    assert requests[0].attributes["tesi.request.timing.first_text_seconds"] >= 0
    assert (
        agent.attributes["tesi.agent.timing.final_response_first_text_seconds"]
        >= (agent.attributes["tesi.agent.timing.first_any_text_seconds"])
    )
    assert requests[1].attributes["gen_ai.response.finish_reasons"] == ("stop",)
    assert requests[1].attributes["tesi.request.context.tool_result_part_count"] == 1
    assert agent.attributes["tesi.agent.usage.input_tokens_total"] == 270
    assert agent.attributes["tesi.agent.usage.max_request_input_tokens"] == 170
    assert agent.attributes["tesi.agent.request_count"] == 2
    assert agent.attributes["tesi.agent.tools.completed"] == 1
    assert "llm.token_count.prompt" not in agent.attributes
    assert sum(s.attributes.get("llm.token_count.prompt", 0) for s in spans) == 270
    assert tool.attributes["tool.name"] == "leggi_evidenze"
    assert tool.attributes["tesi.tool.status"] == "completed"
    assert tool.attributes["tesi.tool.results_count"] == 1
    assert tool.end_time >= tool.start_time
    assert "Non riferisce dolore." not in "\n".join(s.to_json() for s in spans)
    assert len(calls) == 2
    assert (path / "risposta.parziale.json").read_text() == final
    report = json.loads((path / "esito.json").read_text())
    assert report["richieste_llm_avviate"] == 2
    assert report["consumo"]["metrics"]["input_tokens"] == 270
    assert report["consumo"]["metrics"]["output_tokens"] == 50
    assert len(report["consumo"]["step_budgets"]) == 2
    budgets = report["consumo"]["step_budgets"]
    assert budgets[1]["input_tokens_estimate"] < budgets[0]["input_tokens_estimate"] + 2000
    assert report["consumo"]["tools"]["consulted_evidence_ids"] == ["evidenza-0001"]
    assert "Consulto il passaggio" not in (path / "bozza.txt").read_text()


def test_default_execution_has_no_total_deadline_and_propagates_nested_timeouts(monkeypatch):
    from codice_tesi.discharge import limits

    def unexpected_timer(*args):
        pytest.fail("Nessuna scadenza complessiva deve essere creata per default")

    monkeypatch.setattr(limits.asyncio, "timeout", unexpected_timer)

    async def check():
        async with limits.execution_deadline(ModelConfig().total_timeout):
            await asyncio.sleep(0)
        with pytest.raises(TimeoutError, match="rete"):
            async with limits.execution_deadline(None):
                raise TimeoutError("rete")

    asyncio.run(check())


@pytest.mark.parametrize("first_result", ["read_ok", "analysis_ok", "schema_error", "read_error"])
def test_large_context_reserves_the_next_response_for_final_text(
    packet_factory, letter_factory, response_factory, monkeypatch, tmp_path, first_result
):
    from types import SimpleNamespace

    final = response_factory(letter_factory())
    requests = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, first):
            self.first = first

        async def __aiter__(self):
            if self.first:
                args = {
                    "read_ok": {"ids": ["evidenza-0001"]},
                    "analysis_ok": {"esame": "Sodio"},
                    "schema_error": {"ids": "bad"},
                    "read_error": {"ids": ["missing-evidence"]},
                }[first_result]
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "first",
                                "type": "function",
                                "function": {
                                    "name": "riepiloga_osservazioni"
                                    if first_result == "analysis_ok"
                                    else "leggi_evidenze",
                                    "arguments": json.dumps(args),
                                },
                            }
                        ]
                    },
                    "tool_calls",
                    {"prompt_tokens": 40000, "completion_tokens": 20, "total_tokens": 40020},
                )
            else:
                yield chunk(
                    {"content": final},
                    "stop",
                    {"prompt_tokens": 40000, "completion_tokens": 1000, "total_tokens": 41000},
                )
            yield b"data: [DONE]\n\n"

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body.get("tool_choice") == "auto"
        if len(requests) == 2:
            assert body["tools"] == requests[0]["tools"]
            assert body["messages"][:2] == requests[0]["messages"]
            assert body["messages"][-1]["role"] == "user"
            assert "Fase finale obbligatoria" in body["messages"][-1]["content"]
            assert "Dolore da tre giorni." in body["messages"][1]["content"]
            assert any(m["role"] == "assistant" and m.get("tool_calls") for m in body["messages"])
            results = [m for m in body["messages"] if m["role"] == "tool"]
            assert len(results) == 1
            if first_result == "analysis_ok":
                assert "documented_count" in results[0]["content"]
                assert "139" in results[0]["content"]
            if first_result == "read_ok":
                assert "Dolore da tre giorni." in results[0]["content"]
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=Stream(len(requests) == 1)
        )

    def client(*args, **kwargs):
        return AsyncOpenAI(
            base_url="http://test/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(api, "create_client", client)
    monkeypatch.setattr(
        "codice_tesi.discharge.generator.estimate_input_tokens",
        lambda *a, **kw: SimpleNamespace(tokens=40000),
    )
    monkeypatch.setenv("TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("HARDWARE_ENABLED", "false")
    directory = asyncio.run(
        application.run_application(
            backend_url="http://test",
            output_dir=tmp_path,
            packet=packet_factory(
                "dati_ricovero",
                text="Dolore da tre giorni.\n"
                "2026-11-01 | Observation | Sodio | final | 139 mmol/L\n",
            ),
            console=False,
            config=ModelConfig(thinking=False, tools_enabled=True),
        )
    )
    report = json.loads((directory / "esito.json").read_text())
    assert report["stato"] == "bozza_da_revisionare" and len(requests) == 2
    assert report["consumo"]["step_budgets"][1]["final_only_reason"] == "token_budget"
    assert report["consumo"]["step_budgets"][1]["history_mode"] == "final_append"
    assert report["consumo"]["metrics"]["total_tokens"] == 81020
    for sample, body in zip(report["consumo"]["metrics"]["requests"], requests, strict=True):
        assert sample["request"]["max_tokens"] == body["max_tokens"]
        assert sample["request"]["tools_available"] == len(body.get("tools", []))
    assert report["consumo"]["metrics"]["requests"][1]["request"]["history_mode"] == "final_append"
    if first_result == "read_error":
        assert report["consumo"]["tools"]["calls"][0]["error_code"] == "unknown_evidence"


@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("unknown_tool", [False, True])
def test_invalid_tool_arguments_are_corrected_once_and_audited_without_content(
    packet_factory,
    letter_factory,
    response_factory,
    monkeypatch,
    tmp_path,
    traced_run,
    repair,
    unknown_tool,
):
    final = response_factory(letter_factory())
    requests = []
    rejected_name = "tool_sconosciuto" if unknown_tool else "leggi_evidenze"

    class Stream(httpx.AsyncByteStream):
        def __init__(self, index):
            self.index = index

        async def __aiter__(self):
            if self.index <= 2:
                args = (
                    {"ids": ["evidenza-0001"]}
                    if repair and self.index == 2
                    else {"ids": "PRIVATE_BAD_ARGUMENTS"}
                )
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call-{self.index}",
                            "type": "function",
                            "function": {
                                "name": "leggi_evidenze"
                                if not unknown_tool or (repair and self.index == 2)
                                else "PRIVATE_UNKNOWN_TOOL",
                                "arguments": json.dumps(args),
                            },
                        }
                    ]
                }
                yield chunk(
                    delta,
                    "tool_calls",
                    {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                )
            else:
                yield chunk(
                    {"content": final},
                    "stop",
                    {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                )
            yield b"data: [DONE]\n\n"

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["thinking"] == {"type": "disabled"}
        if len(requests) == 2:
            feedback = [m for m in body["messages"] if m["role"] == "tool"]
            assert len(feedback) == 1
            assert ("Available tools" if unknown_tool else "ids") in feedback[0]["content"]
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=Stream(len(requests))
        )

    def client(*args, **kwargs):
        return AsyncOpenAI(
            base_url="http://test/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(api, "create_client", client)
    monkeypatch.setenv("HARDWARE_ENABLED", "false")
    run = application.run_application(
        backend_url="http://test",
        output_dir=tmp_path,
        packet=packet_factory("nota"),
        console=False,
        config=ModelConfig(thinking=False, tools_enabled=True, max_tokens=8192),
    )
    if repair:
        result = asyncio.run(run)
        assert (result / "bozza_strutturata.json").exists()
        assert len(requests) == 3
    else:
        with pytest.raises(ToolCallValidationError, match=rejected_name):
            asyncio.run(run)
        assert len(requests) == 2
        assert not list(tmp_path.rglob("bozza.txt"))
    report = json.loads(next(tmp_path.rglob("esito.json")).read_text())
    attempts = report["consumo"]["tools"]["attempts"]
    assert attempts == [
        {"tool": rejected_name, "args_valid": False},
        {"tool": "leggi_evidenze" if repair else rejected_name, "args_valid": repair},
    ]
    assert len(report["consumo"]["tools"]["calls"]) == int(repair)
    _, exporter = traced_run
    spans = exporter.get_finished_spans()
    rejections = [s for s in spans if s.attributes.get("tesi.tool.status") == "rejected"]
    assert len(rejections) == (1 if repair else 2)
    assert all(s.status.status_code == StatusCode.ERROR for s in rejections)
    assert "PRIVATE_BAD_ARGUMENTS" not in json.dumps(report)
    assert "PRIVATE_BAD_ARGUMENTS" not in "\n".join(s.to_json() for s in spans)
    assert "PRIVATE_UNKNOWN_TOOL" not in json.dumps(report)
    assert "PRIVATE_UNKNOWN_TOOL" not in "\n".join(s.to_json() for s in spans)


def test_total_deadline_stops_continuous_reasoning_and_preserves_partial(packet_factory):
    async def check():
        closed = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    await asyncio.sleep(0.01)
                    yield chunk({"reasoning_content": "progresso"})

            async def aclose(self):
                closed.append(True)

        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=Stream()
            )
        )
        async with AsyncOpenAI(
            base_url="http://test/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=transport),
        ) as client:
            config = ModelConfig(total_timeout=0.15, first_text_timeout=0.1)
            generator = LLMDraftGenerator(config, client)
            request = prepare_request(packet_factory("nota"), ContextConfig(), output_tokens=8192)
            with pytest.raises(ExecutionTimeoutError):
                _ = [part async for part in generator.stream(request)]
            assert generator.usage["stream"]["caratteri_ragionamento"] > 0
            assert generator.usage["metrics"]["model_requests_started"] == 1
            assert closed

    asyncio.run(check())


def test_prepared_catalog_is_frozen_and_foreign_catalog_is_rejected(packet_factory):
    packet = packet_factory("nota")
    catalog = build_catalog(packet)
    old = EvidenceCatalog(sources=catalog.sources, evidences=catalog.evidences)
    request = prepare_request(packet, ContextConfig(), output_tokens=8192, catalog=old)
    assert request.catalog.segmentation_version == "fonte-completa-v1"
    other = build_catalog(packet_factory("nota", text="Fonte differente."))
    with pytest.raises(ValueError, match="fonti"):
        prepare_request(packet, ContextConfig(), output_tokens=8192, catalog=other)


def test_application_deadline_includes_packet_fetch_and_records_failure(monkeypatch, tmp_path):
    async def fetch(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(application.PacketClient, "fetch", fetch)
    monkeypatch.setenv("TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("HARDWARE_ENABLED", "false")
    with pytest.raises(ExecutionTimeoutError):
        asyncio.run(
            application.run_application(
                backend_url="http://test",
                output_dir=tmp_path,
                config=ModelConfig(total_timeout=0.03),
                console=False,
            )
        )
    record = json.loads(next((tmp_path / "registro").glob("*.json")).read_text())
    assert record["status"] == "failed" and record["error_type"] == "ExecutionTimeoutError"


@pytest.mark.parametrize(
    "operation,args",
    [
        ("riepiloga_osservazioni", {"esame": "PRIVATE_LABEL"}),
        ("ricostruisci_terapia_documentata", {"farmaco": "PRIVATE_LABEL"}),
        ("ricostruisci_sequenza_eventi", {}),
    ],
)
def test_derived_tool_spans_share_trace_and_export_only_safe_counters(
    packet_factory, traced_run, operation, args
):
    telemetry, exporter = traced_run
    catalog = build_catalog(
        packet_factory(
            "dati_ricovero",
            text=(
                "2026-11-01 | Observation | PRIVATE_LABEL | final | 139 mmol/L\n"
                "2026-11-01 | MedicationRequest | PRIVATE_LABEL | stato nello snapshot=active\n"
            ),
        )
    )
    tools = DocumentaryTools(catalog, extract_facts(catalog), telemetry=telemetry)
    with telemetry.span("generation"):
        result = tools.execute(operation, **args)
    spans = exporter.get_finished_spans()
    parent = next(s for s in spans if s.name == "generation")
    span = next(s for s in spans if s.attributes.get("openinference.span.kind") == "TOOL")
    assert span.parent.span_id == parent.context.span_id
    assert span.context.trace_id == parent.context.trace_id
    assert span.attributes["tool.name"] == operation
    assert span.attributes["tesi.tool.status"] == "completed"
    assert span.attributes["tesi.tool.result_bytes"] == tools._size(result)
    assert span.attributes["tesi.tool.results_count"] == len(result["items"])
    for private in ("PRIVATE_LABEL", "139", "mmol/L", "evidenza-0001", "source-0"):
        assert private not in json.dumps(dict(span.attributes))
    assert not span.events
    assert "PRIVATE_LABEL" not in json.dumps(tools.audit())


@pytest.mark.parametrize("invalid_args", [False, True])
def test_final_phase_keeps_prefix_and_blocks_more_reads_without_model_retry(
    packet_factory, monkeypatch, tmp_path, invalid_args
):
    from codice_tesi.discharge.tools import DocumentaryTools

    requests, reads = [], []
    original = DocumentaryTools._execute

    def read(self, *args, **kwargs):
        reads.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DocumentaryTools, "_execute", read)

    class Stream(httpx.AsyncByteStream):
        def __init__(self, index):
            self.index = index

        async def __aiter__(self):
            args = (
                {"ids": "INVALID_FINAL_ARGS"}
                if invalid_args and self.index == 2
                else {"ids": ["evidenza-0001"]}
            )
            yield chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"read-{self.index}",
                            "type": "function",
                            "function": {"name": "leggi_evidenze", "arguments": json.dumps(args)},
                        }
                    ]
                },
                "tool_calls",
                {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            )
            yield b"data: [DONE]\n\n"

    def handler(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= 2
        if len(requests) == 2:
            assert requests[1]["tools"] == requests[0]["tools"]
            assert requests[1]["messages"][:2] == requests[0]["messages"]
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=Stream(len(requests))
        )

    def client(*args, **kwargs):
        return AsyncOpenAI(
            base_url="http://fixture/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(api, "create_client", client)
    monkeypatch.setenv("TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("HARDWARE_ENABLED", "false")
    with pytest.raises(FinalPhaseToolCallError):
        asyncio.run(
            application.run_application(
                backend_url="http://fixture",
                packet=packet_factory("nota"),
                output_dir=tmp_path,
                console=False,
                config=ModelConfig(tools_enabled=True, max_requests=2, max_tokens=8192),
            )
        )
    assert len(requests) == 2 and len(reads) == 1
    report = json.loads(next(tmp_path.rglob("esito.json")).read_text())
    assert report["stato"] == "fallito"
    assert report["consumo"]["metrics"]["model_requests_started"] == 2
    assert report["consumo"]["tools"]["consulted_evidence_ids"] == ["evidenza-0001"]
    assert not list(tmp_path.rglob("bozza.txt"))
