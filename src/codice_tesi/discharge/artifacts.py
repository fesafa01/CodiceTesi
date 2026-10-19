"""Una richiesta, una bozza TXT progressiva e un esito tecnico sintetico."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from codice_tesi.config import ModelConfig
from codice_tesi.discharge.context import PreparedRequest
from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.prompts import PROMPT_VERSION


class RunArtifacts:
    def __init__(
        self,
        output_dir: Path,
        packet: CompositionPacket,
        request: PreparedRequest,
        *,
        seed: int,
        fixed_context: bool,
        config: ModelConfig,
        preview: bool = False,
    ):
        name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        self.directory = output_dir / name
        self.directory.mkdir(parents=True, exist_ok=False)
        self.partial = self.directory / "bozza.parziale.txt"
        self.draft = self.directory / "bozza.txt"
        self.request = self.directory / "richiesta.txt"
        self.request.write_text(
            "ISTRUZIONI\n\n" + request.instructions + "\n\nCONTESTO E NOTE\n\n" + request.prompt,
            encoding="utf-8",
        )
        self.report = {
            "stato": "preparato",
            "contesto": packet.context.model_dump(mode="json"),
            "seed_selezione": None if fixed_context else seed,
            "template": {
                "id": packet.template.id,
                "version": packet.template.version,
                "sha256": packet.template.sha256,
            },
            "policy_version": packet.policy_version,
            "prompt_version": PROMPT_VERSION,
            "config_modello": config.model_dump(mode="json"),
            "modalita": "anteprima" if preview else "generazione",
            "richieste_llm_previste": 1,
            "richieste_llm_avviate": 0,
            "campi_previsti": [section.id for section in packet.template.sections],
            "input_tokens_stimati": request.input_tokens_estimate,
            "fonti": [
                s.model_dump(mode="json", exclude={"text"}) for s in packet.sections[0].sources
            ],
        }
        self.finish("preparato")

    def start(self) -> None:
        self.report["richieste_llm_avviate"] = 1
        self.finish("in_corso")

    def finish(self, status: str, *, error: BaseException | None = None, usage=None) -> None:
        self.report["stato"] = status
        if error is not None:
            self.report["errore_tipo"] = type(error).__name__
        if usage:
            self.report["consumo"] = dict(usage)
        path = self.directory / "esito.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
