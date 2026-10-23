"""Una richiesta, una bozza TXT progressiva e un esito tecnico sintetico."""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TextIO
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
        context_name = re.sub(r"[^a-zA-Z0-9._-]", "_", packet.context.id).strip(".")[:80]
        if context_name != packet.context.id:
            context_name = (
                (context_name or "contesto")
                + "-"
                + sha256(packet.context.id.encode()).hexdigest()[:8]
            )
        name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        self.directory = output_dir.resolve() / context_name / name
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
            "schema_pacchetto": packet.schema_version,
            "prompt_version": PROMPT_VERSION,
            "config_modello": config.model_dump(mode="json"),
            "modalita": "anteprima" if preview else "generazione",
            "richieste_llm_previste": 1,
            "richieste_llm_avviate": 0,
            "campi_previsti": [section.id for section in packet.template.sections],
            "input_tokens_stimati": request.input_tokens_estimate,
            "fonti": [s.model_dump(mode="json", exclude={"text"}) for s in packet.sources],
        }
        self.finish("preparato")

    @contextmanager
    def open_draft(self, *, validation_context=nullcontext) -> Iterator[TextIO]:
        """Conserva il parziale; lo rinomina dopo lo stream e i controlli di forma."""
        with self.partial.open("x", encoding="utf-8") as output:
            yield output
        with validation_context():
            text = self.partial.read_text(encoding="utf-8")
            if not text.strip():
                raise ValueError("Il modello non ha prodotto testo.")
            self.partial.replace(self.draft)

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
