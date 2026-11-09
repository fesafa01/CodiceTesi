import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from codice_tesi.config import GEMMA_MODEL, MISTRAL_MODEL, ModelConfig
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.models import MIMIC_DATASET, CompositionPacket
from codice_tesi.discharge.provenance import build_catalog
from scripts import experiment_tool_prompt as experiment


@pytest.mark.parametrize("model", [GEMMA_MODEL, MISTRAL_MODEL])
def test_variant_preserves_sources_catalog_facts_and_sampling(packet_factory, model):
    packet = packet_factory("diario", text="Fixture originale: non alterare valori e negazioni.")
    packet = packet.model_copy(
        update={"context": packet.context.model_copy(update={"dataset": MIMIC_DATASET})}
    )
    config = ModelConfig(model=model, thinking=True, tools_enabled=True)
    settings = config.request_settings()
    request = prepare_request(packet, ContextConfig(), output_tokens=32768, model_config=config)
    original_head = request.prompt.rpartition(experiment.PROMPT_MARKER)[0]
    changed = experiment.explicit_request(request, config)
    assert changed.prompt.startswith(original_head)
    assert changed.catalog is request.catalog and changed.facts is request.facts
    assert request.prompt_version == "campus-mimic-v15-tools-v1"
    assert changed.prompt_version.endswith("/explicit-phases-v1")
    assert "Non è obbligatorio chiamare tool." not in changed.instructions
    assert experiment.PROMPT_MARKER not in changed.prompt[len(original_head) :]
    assert experiment.PROCEDURE in changed.instructions
    assert config.request_settings() == settings
    assert settings["openai_reasoning_effort"] == ("high" if model == MISTRAL_MODEL else "low")
    with pytest.raises(ValueError, match="UnexpectedInstructionVersion"):
        experiment.explicit_request(replace(request, instructions="Nuove istruzioni"), config)


def make_item(tmp_path, packet_factory):
    packet = packet_factory("diario", text="Fixture inventata.")
    packet = packet.model_copy(
        update={"context": packet.context.model_copy(update={"dataset": MIMIC_DATASET})}
    )
    directory = tmp_path / "input"
    directory.mkdir()
    hashes = {}
    for name, value in (("packet.json", packet), ("catalog.json", build_catalog(packet))):
        raw = value.model_dump_json().encode()
        (directory / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    return {
        "id": "gemma-hosp-1",
        "case": "Caso Hosp 1",
        "input_dir": "input",
        "input_sha256": hashes,
        "model_config": ModelConfig(model=GEMMA_MODEL, tools_enabled=True).model_dump(),
        "baseline": {"requested": 0},
    }


def test_changed_input_is_rejected_before_generation(tmp_path, packet_factory):
    item = make_item(tmp_path, packet_factory)
    (tmp_path / "input/packet.json").write_text("contenuto modificato")
    with pytest.raises(ValueError, match="InputHashMismatch"):
        experiment.read_input(tmp_path, item, "packet.json", CompositionPacket)


def test_failure_is_recorded_locally_and_attempt_cannot_be_overwritten(
    tmp_path, packet_factory, monkeypatch
):
    item = make_item(tmp_path, packet_factory)
    baseline = tmp_path / "campagna-originale.json"
    baseline.write_text("non cambiare")

    @asynccontextmanager
    async def client(*args):
        from types import SimpleNamespace

        yield SimpleNamespace(_client=SimpleNamespace(event_hooks={"request": []}))

    class Generator:
        usage = {"metrics": {"tool_calls_requested": 0}}
        partial_text = ""
        tool_results = []

        def __init__(self, *args):
            pass

        async def stream(self, request):
            raise RuntimeError("errore simulato")
            yield ""  # pragma: no cover

    monkeypatch.setattr(experiment.api, "create_client", client)
    monkeypatch.setattr(experiment, "LLMDraftGenerator", Generator)
    monkeypatch.setattr(experiment, "capture_run", lambda *args: {"status": "not_collected"})
    result = asyncio.run(experiment.execute(tmp_path, item))
    assert result["status"] == "failed" and result["error_type"] == "RuntimeError"
    record = json.loads((tmp_path / result["record"]).read_text())
    assert record["counts_toward_180"] is False
    assert baseline.read_text() == "non cambiare"
    assert not (tmp_path / "registro").exists()
    with pytest.raises(FileExistsError, match="AttemptAlreadyExists"):
        asyncio.run(experiment.execute(tmp_path, item))
