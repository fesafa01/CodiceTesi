import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from codice_tesi.config import GEMMA_MODEL, MISTRAL_MODEL, QWEN_MODEL
from scripts import spark_campaign as campaign


@pytest.mark.parametrize("model", [QWEN_MODEL, GEMMA_MODEL, MISTRAL_MODEL])
def test_campaign_uses_same_30_cases_in_both_modes_and_resumes(monkeypatch, tmp_path, model):
    contexts = [
        {
            "dataset": campaign.DATASET,
            "dataset_version": "fixture-v1",
            "id": f"fixture-{path}-{n:02d}",
            "display_name": f"Caso {path} {n}",
        }
        for path in ("Hosp", "Hosp e ED")
        for n in range(1, 16)
    ]
    checkpoint = tmp_path / "campaign.json"
    sessions = {}
    started = []
    interrupted = False
    startup_error = False
    polling_error = False

    def request(route, body=None):
        nonlocal interrupted, startup_error, polling_error
        if route == "contesti":
            return contexts
        if route == "preparazioni":
            assert body["expected_dataset_version"] == "fixture-v1"
            session_id = str(UUID(int=len(sessions) + 1))
            sessions[session_id] = {"case": body["context_id"], "options": None}
            return {"id": session_id}
        _, session_id, action = route.split("/")
        session = sessions[session_id]
        if action == "genera":
            if session["options"] is None:
                session["options"] = body
                started.append((session["case"], body))
            assert session["options"] == body
            if not startup_error:
                startup_error = True
                raise campaign.ApiError(502, "genera")
            return {"status": "running"}
        assert action == "stato?summary=true"
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        if not polling_error:
            polling_error = True
            raise campaign.ApiError(503, "stato")
        return {"status": "completed", "run_id": f"run-{session_id}"}

    monkeypatch.setattr(campaign, "sleep", lambda _: None)
    with pytest.raises(KeyboardInterrupt):
        campaign.execute_campaign(request, checkpoint, model=model)
    assert len(sessions) == len(started) == 1

    assert campaign.execute_campaign(request, checkpoint, model=model) == 0
    assert campaign.execute_campaign(request, checkpoint, model=model) == 0
    assert len(sessions) == len(started) == 60
    cases = sorted(case["id"] for case in contexts)
    assert started == [
        (
            case_id,
            {
                "thinking": enabled,
                "tools_enabled": enabled,
                "expected_model": model,
                "inactivity_timeout_seconds": 600,
            },
        )
        for enabled in (False, True)
        for case_id in cases
    ]
    state = json.loads(checkpoint.read_text())
    assert state["model"] == model
    assert all(run["status"] == "completed" for run in state["runs"].values())
    assert checkpoint.stat().st_mode & 0o077 == 0
    for case in contexts:
        case["dataset_version"] = "fixture-v2"
    with pytest.raises(campaign.CampaignError, match="altro modello o campione"):
        campaign.execute_campaign(request, checkpoint, model=model)


def test_campaign_retries_are_bounded_and_report_the_operation(monkeypatch):
    monkeypatch.setattr(campaign, "sleep", lambda _: None)
    requests = []

    def request(route, body=None):
        requests.append((route, body))
        raise campaign.ApiError(502, "genera")

    with pytest.raises(campaign.ApiError, match="genera, HTTP 502"):
        campaign.start_run(request, str(UUID(int=1)), False, model=QWEN_MODEL)
    assert len(requests) == 6 and requests == [requests[0]] * 6


def test_campaign_checks_roster_before_creating_sessions(tmp_path):
    def request(route, body=None):
        assert route == "contesti"
        return []

    checkpoint = tmp_path / "campaign.json"
    with pytest.raises(campaign.CampaignError, match="15 casi Hosp e 15 casi Hosp e ED"):
        campaign.execute_campaign(request, checkpoint, model=QWEN_MODEL)
    assert not checkpoint.exists()


def test_report_shows_failure_codes_without_contacting_api_or_printing_ids(
    monkeypatch, tmp_path, capsys
):
    checkpoint = tmp_path / "campaign.json"
    campaign.save_checkpoint(
        checkpoint,
        {
            "model": QWEN_MODEL,
            "cases": [f"fixture-case-{n}" for n in range(30)],
            "runs": {
                "1": {
                    "status": "failed",
                    "failure_code": "model_protocol_error",
                    "session_id": "fixture-session",
                    "run_id": "fixture-run",
                },
                "2": {"status": "completed"},
                "3": {"status": "prepared"},
            },
        },
    )
    monkeypatch.setattr(campaign.sys, "argv", ["campaign", "--report", "--state", str(checkpoint)])
    monkeypatch.setattr(campaign, "local_api", lambda _: pytest.fail("Il report è locale"))
    assert campaign.main(QWEN_MODEL, "campagna-qwen-60.json") == 0
    output = capsys.readouterr().out
    assert "[1/60] base — failed (model_protocol_error)" in output
    assert "1/60 completati, 1 non riusciti, 58 da terminare" in output
    assert "fixture-" not in output


@pytest.mark.parametrize(
    ("name", "model"), [("qwen", QWEN_MODEL), ("gemma", GEMMA_MODEL), ("mistral", MISTRAL_MODEL)]
)
def test_launcher_reads_its_own_checkpoint(monkeypatch, tmp_path, name, model):
    monkeypatch.setenv("TESI_PRIVATE_DIR", str(tmp_path))
    campaign.save_checkpoint(
        tmp_path / f"campagna-{name}-60.json",
        {
            "model": model,
            "cases": [f"fixture-{n}" for n in range(30)],
            "runs": {"1": {"status": "completed"}},
        },
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / f"run_{name}_campaign.py"
    result = subprocess.run(
        [sys.executable, str(script), "--report"], capture_output=True, text=True, check=True
    )
    assert "1/60 completati, 0 non riusciti, 59 da terminare" in result.stdout


@pytest.mark.parametrize("model", [QWEN_MODEL, GEMMA_MODEL, MISTRAL_MODEL])
@pytest.mark.parametrize("conditions", [campaign.REASONING_CONDITIONS, ("base",)])
def test_new_suite_runs_only_selected_conditions(monkeypatch, tmp_path, model, conditions):
    contexts = [
        {
            "dataset": campaign.DATASET,
            "dataset_version": "synthetic-v1",
            "id": f"synthetic-{path}-{n}",
            "display_name": f"Caso {path} {n}",
        }
        for path in ("Hosp", "Hosp e ED")
        for n in range(1, 16)
    ]
    starts, sessions = [], {}

    def request(route, body=None):
        if route == "contesti":
            return contexts
        if route == "preparazioni":
            sid = str(UUID(int=len(sessions) + 1))
            sessions[sid] = body["context_id"]
            return {"id": sid}
        _, sid, action = route.split("/")
        assert action == "genera"  # Risposta conclusa: nessuna lettura di fonti o bozze.
        starts.append((sessions[sid], body))
        return {"status": "completed", "run_id": f"synthetic-{sid}"}

    monkeypatch.setattr(campaign, "sleep", lambda _: None)
    path = tmp_path / "new-suite.json"
    options = {
        "model": model,
        "conditions": conditions,
        "tool_version": campaign.TOOL_EXTRACTION_VERSION,
    }
    assert campaign.execute_campaign(request, path, **options) == 0
    assert campaign.execute_campaign(request, path, **options) == 0
    assert len(starts) == 30 * len(conditions)
    if len(conditions) == 2:
        assert [case for case, _ in starts[:30]] == [case for case, _ in starts[30:]]
    for index, (_, body) in enumerate(starts):
        assert body["thinking"] is (conditions != ("base",))
        assert body["tools_enabled"] is (index >= 30)
        assert body["expected_model"] == model
        assert body["expected_tool_extraction_version"] == "mimic-record-v1"
    state = json.loads(path.read_text())
    assert state["conditions"] == list(conditions)
    with pytest.raises(campaign.CampaignError, match="altro modello o campione"):
        campaign.execute_campaign(request, path, model=model)


def test_new_suite_cannot_resume_a_legacy_checkpoint(tmp_path):
    contexts = [
        {
            "dataset": campaign.DATASET,
            "dataset_version": "synthetic-v1",
            "id": f"synthetic-{path}-{n}",
            "display_name": f"Caso {path} {n}",
        }
        for path in ("Hosp", "Hosp e ED")
        for n in range(1, 16)
    ]
    checkpoint = tmp_path / "legacy.json"
    campaign.save_checkpoint(
        checkpoint,
        {
            "model": QWEN_MODEL,
            "dataset_version": "synthetic-v1",
            "cases": sorted(c["id"] for c in contexts),
            "runs": {},
        },
    )

    def request(route, body=None):
        assert route == "contesti"
        return contexts

    with pytest.raises(campaign.CampaignError, match="altro modello o campione"):
        campaign.execute_campaign(
            request,
            checkpoint,
            model=QWEN_MODEL,
            conditions=campaign.REASONING_CONDITIONS,
            tool_version=campaign.TOOL_EXTRACTION_VERSION,
        )


@pytest.mark.parametrize("name", ["qwen", "gemma", "mistral"])
def test_new_suite_launcher_uses_new_checkpoint_and_offline_report(monkeypatch, tmp_path, name):
    models = {"qwen": QWEN_MODEL, "gemma": GEMMA_MODEL, "mistral": MISTRAL_MODEL}
    monkeypatch.setenv("TESI_PRIVATE_DIR", str(tmp_path))
    campaign.save_checkpoint(
        tmp_path / f"campagna-{name}-reasoning-tools-v1.json",
        {
            "model": models[name],
            "conditions": list(campaign.REASONING_CONDITIONS),
            "tool_extraction_version": campaign.TOOL_EXTRACTION_VERSION,
            "cases": [f"synthetic-{n}" for n in range(30)],
            "runs": {"1": {"status": "failed", "failure_code": "model_protocol_error"}},
        },
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / f"run_{name}_campaign.py"
    result = subprocess.run(
        [sys.executable, str(script), "--suite", "reasoning", "--report"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[1/60] reasoning — failed" in result.stdout
    assert "0/60 completati, 1 non riusciti, 59 da terminare" in result.stdout


def test_suite_version_rejection_is_not_retried(monkeypatch):
    calls = []

    def request(route, body=None):
        calls.append(body)
        raise campaign.ApiError(412, "genera")

    with pytest.raises(campaign.CampaignError, match="versione dei tool attesa"):
        campaign.start_run(
            request,
            str(UUID(int=1)),
            True,
            model=QWEN_MODEL,
            tool_version=campaign.TOOL_EXTRACTION_VERSION,
        )
    assert len(calls) == 1
