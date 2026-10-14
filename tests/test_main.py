"""Controlli dell’entry point del modello."""

import pytest

from codice_tesi import main


def test_blank_prompt_is_rejected_before_contacting_model(capsys):
    with pytest.raises(SystemExit) as exc:
        main.main(["agente", "   "])
    assert exc.value.code == 2
    assert "il prompt non può essere vuoto" in capsys.readouterr().err
