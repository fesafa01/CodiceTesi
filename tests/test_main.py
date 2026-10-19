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


def test_documented_flags_are_passed_to_application(monkeypatch):
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
        ]
    )
    assert calls[0]["context_id"] == "episode-x" and calls[0]["seed"] is None
    assert calls[0]["output_dir"] == Path("var/bozze")
    assert calls[0]["backend_url"] == "http://localhost:8001"
    assert calls[0]["context_config"].window_tokens == 24000 and calls[0]["dry_run"]
