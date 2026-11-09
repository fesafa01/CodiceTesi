import json
from pathlib import Path

import pytest

from scripts.report_reasoning_suite import summarize_archives


def test_report_reads_only_technical_files_and_omits_content(tmp_path, monkeypatch):
    for tools in (False, True):
        path = tmp_path / str(tools)
        path.mkdir()
        card = {
            "model": "qwen3.8:27b",
            "context_id": "synthetic-case",
            "run_id": str(tools),
            "reasoning_enabled": True,
            "tools_enabled": tools,
            "status": "completed",
            "draft_technical_validation": "passed",
        }
        (path / "scheda.json").write_text(json.dumps(card))
        summary = {
            "extraction_version": "mimic-record-v1",
            "recognized_facts": 5,
            "requested": 2,
            "completed": 2,
            "failed": 0,
            "arguments_rejected": 0,
            "empty": 1,
            "nonempty": 1,
        }
        (path / "metriche.json").write_text(
            json.dumps(
                {
                    "run_id": str(tools),
                    "token_e_stream": {"tool_summary": summary} if tools else {},
                }
            )
        )
        for name in ("risultati_tool.json", "bozza.txt", "evidenze.md", "lettera_ufficiale.txt"):
            (path / name).write_text("NON LEGGERE: contenuto sintetico escluso")
    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path.name in {"scheda.json", "metriche.json"}
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    result = summarize_archives([tmp_path])
    tool_row = next(row for row in result if row["condition"] == "reasoning-tools")
    assert tool_row["tools_nonempty"] == tool_row["tools_empty"] == 1
    assert tool_row["tests_with_tool_requests"] == 1
    assert "synthetic-case" not in json.dumps(result)
    assert "NON LEGGERE" not in json.dumps(result)
    with pytest.raises(ValueError, match="Più tentativi"):
        summarize_archives([tmp_path, tmp_path])
