"""Validazione del contratto locale su file temporanei, indipendente da casi clinici."""

import hashlib
import json

import pytest

from codice_tesi.backend.cli import main
from codice_tesi.backend.config import BackendSettings
from codice_tesi.backend.dataset import Dataset


def update(directory, file, **changes):
    path = directory / file
    content = json.loads(path.read_text())
    content.update(changes)
    path.write_text(json.dumps(content))


def test_default_configuration_has_no_dataset():
    assert BackendSettings(_env_file=None).dataset_dir is None


def test_initial_scenario_is_read_from_manifest(dataset_dir):
    dataset = Dataset(dataset_dir)
    loaded = dataset.load()
    assert loaded[0]["scenario_iniziale"] == "baseline"
    assert len(loaded[2]) == 2
    assert dataset.scenarios["baseline"][2]
    update(dataset_dir, "manifest.json", scenario_iniziale="absent")
    with pytest.raises(ValueError, match="scenario iniziale"):
        Dataset(dataset_dir).load()


def test_document_integrity_and_extraction_are_checked(dataset_dir):
    dataset = Dataset(dataset_dir)
    loaded = dataset.load()
    report = next(s for s in loaded[6] if s.identity["id"] == "report-a")
    assert report.sha256 == hashlib.sha256(report.pdf).hexdigest()
    assert report.sections[0]["pagina"] == 1
    with (dataset_dir / "report.pdf").open("ab") as file:
        file.write(b"tampered")
    with pytest.raises(ValueError, match="Integrità"):
        Dataset(dataset_dir).load()


@pytest.mark.parametrize("file", ["../outside.json", "/tmp/outside.json", "note.txt"])
def test_loader_rejects_untrusted_paths(dataset_dir, file):
    with pytest.raises((ValueError, FileNotFoundError)):
        Dataset(dataset_dir).read(file)


@pytest.mark.parametrize("payload", ['{"id":"a","id":"b"}', '{"value":NaN}'])
def test_loader_rejects_ambiguous_json(dataset_dir, payload):
    (dataset_dir / "note-a.json").write_text(payload)
    with pytest.raises(ValueError):
        Dataset(dataset_dir).load()


def test_fhir_input_requires_a_dedicated_adapter(dataset_dir):
    (dataset_dir / "note-a.json").write_text('{"resourceType":"Patient","id":"a"}')
    with pytest.raises(ValueError, match="adattatore"):
        Dataset(dataset_dir).load()


def test_source_cannot_be_assigned_to_another_patient(dataset_dir):
    update(dataset_dir, "note-a.json", paziente_id="patient-b")
    with pytest.raises(ValueError, match="paziente o episodio incoerente"):
        Dataset(dataset_dir).load()


@pytest.mark.parametrize("command", ["validate-dataset", "import-dataset"])
def test_import_commands_require_an_explicit_path(monkeypatch, capsys, command):
    monkeypatch.delenv("CLINICAL_DATASET_DIR", raising=False)
    with pytest.raises(SystemExit) as exc:
        main([command])
    assert exc.value.code == 2
    assert "--dataset-dir" in capsys.readouterr().err


def test_validation_with_an_explicit_dataset(dataset_dir, capsys):
    main(["--dataset-dir", str(dataset_dir), "validate-dataset"])
    result = json.loads(capsys.readouterr().out)
    assert result["scenari"] == ["baseline", "offline", "updates"]
