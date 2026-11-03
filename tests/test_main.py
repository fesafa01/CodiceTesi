"""La CLI espone solo compilazione e verifica dei modelli disponibili."""

import pytest

from codice_tesi import main


def test_free_prompt_command_is_not_available(capsys):
    with pytest.raises(SystemExit) as exc:
        main.main(["agente", "Rispondi OK"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_negative_seed_is_rejected_before_contacting_backend(capsys):
    with pytest.raises(SystemExit) as exc:
        main.main(["dimissione", "--seed", "-1"])
    assert exc.value.code == 2
    assert "seed deve essere non negativo" in capsys.readouterr().err


def test_context_and_seed_cannot_be_combined(capsys):
    with pytest.raises(SystemExit) as exc:
        main.main(["dimissione", "--contesto", "episode-x", "--seed", "42"])
    assert exc.value.code == 2
    assert "not allowed" in capsys.readouterr().err


def test_wrapped_model_timeout_has_actionable_message(monkeypatch, capsys):
    import httpx2 as httpx
    from openai import APITimeoutError
    from pydantic_ai.exceptions import ModelAPIError

    from codice_tesi import application

    async def run(**kwargs):
        try:
            raise APITimeoutError(request=httpx.Request("POST", "http://test/v1"))
        except APITimeoutError as exc:
            raise ModelAPIError(model_name="test", message=exc.message) from exc

    monkeypatch.setattr(application, "run_application", run)
    with pytest.raises(SystemExit) as exc:
        main.main(["dimissione"])
    assert exc.value.code == 1
    error = capsys.readouterr().err
    assert "Timeout dello Spark" in error and "Traceback" not in error


@pytest.mark.parametrize(
    "thinking_flag, expected", [("--thinking", True), ("--no-thinking", False)]
)
def test_documented_flags_are_passed_to_application(monkeypatch, thinking_flag, expected):
    from pathlib import Path

    from codice_tesi import application

    calls = []

    async def run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(application, "run_application", run)
    main.main(
        [
            "dimissione",
            "--contesto",
            "episode-x",
            "--output-dir",
            "var/bozze",
            "--backend-url",
            "http://localhost:8001",
            "--context-window",
            "24000",
            "--dry-run",
            thinking_flag,
        ]
    )
    assert calls[0]["context_id"] == "episode-x" and calls[0]["seed"] is None
    assert calls[0]["output_dir"] == Path("var/bozze")
    assert calls[0]["backend_url"] == "http://localhost:8001"
    assert calls[0]["context_config"].window_tokens == 24000 and calls[0]["dry_run"]
    assert calls[0]["config"].thinking is expected
    assert calls[0]["config"].total_timeout is None


def test_absent_thinking_flag_respects_python_configuration(monkeypatch):
    from codice_tesi import application, config

    calls = []
    configured = config.ModelConfig(thinking=False)

    async def run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(config, "ModelConfig", lambda: configured)
    monkeypatch.setattr(application, "run_application", run)
    main.main(["dimissione"])
    assert calls[0]["config"] is configured
