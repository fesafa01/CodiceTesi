import json
from types import SimpleNamespace

from codice_tesi.config import ModelConfig
from codice_tesi.discharge.tools import DocumentaryTools
from scripts.live_tool_smoke import EXPECTED, assess, build_probe


def test_live_probe_grades_real_tool_roundtrip_on_invented_fixture_only():
    request = build_probe(ModelConfig(tools_enabled=True))
    tools = DocumentaryTools(request.catalog, request.facts)
    tools.execute("riepiloga_osservazioni", esame="Parametro Inventato")
    tools.execute("ricostruisci_terapia_documentata", farmaco="Preparato Inventato Z")
    tools.execute("ricostruisci_sequenza_eventi")
    tools.execute("leggi_evidenze", ids=["evidenza-0001"])
    generator = SimpleNamespace(
        usage={
            "tools": tools.audit(),
            "metrics": {
                "requests": [{"request": {"tool_return_parts": 4}}],
                "model_requests_started": 2,
                "tool_summary": tools.summary(),
            },
        },
        tool_results=tools.results,
    )
    assert assess(generator, json.dumps(EXPECTED))["passed"]
    wrapped = assess(generator, "```json\n" + json.dumps(EXPECTED) + "\n```")
    assert wrapped["passed"] and wrapped["accepted_markdown_wrapper"]
    wrong = {**EXPECTED, "stato_evento_emar": "Given"}
    assert not assess(generator, json.dumps(wrong))["passed"]
    generator.usage["metrics"]["requests"] = []
    assert not assess(generator, json.dumps(EXPECTED))["passed"]
