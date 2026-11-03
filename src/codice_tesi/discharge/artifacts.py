"""Stream JSON conservato, citazioni controllate e viste locali della stessa bozza."""

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
from codice_tesi.discharge.provenance import (
    TRACE_SCHEMA_VERSION,
    TraceBinding,
    TraceResult,
    parse_draft,
    render_draft,
    render_evidences,
)
from codice_tesi.discharge.validation import validate_draft


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
        self.template = packet.template
        self.packet = packet
        self.catalog = request.catalog
        self.facts = request.facts
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
        self.partial = self.directory / "risposta.parziale.json"
        self.draft = self.directory / "bozza.txt"
        self.structured = self.directory / "bozza_strutturata.json"
        self.evidences = self.directory / "evidenze.md"
        self.catalog_path = self.directory / "catalogo_evidenze.json"
        self.catalog_path.write_text(
            self.catalog.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        self.facts_path = self.directory / "fatti_documentali.json"
        self.facts_path.write_text(self.facts.model_dump_json(indent=2) + "\n", encoding="utf-8")
        self.request = self.directory / "richiesta.txt"
        self.request.write_text(
            "ISTRUZIONI\n\n" + request.instructions + "\n\nCONTESTO E NOTE\n\n" + request.prompt,
            encoding="utf-8",
        )
        self.report = {
            "stato": "preparato",
            "run_id": uuid4().hex,
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
            "richieste_llm_previste": config.max_requests if config.tools_enabled else 1,
            "richieste_llm_avviate": 0,
            "campi_previsti": [section.id for section in packet.template.sections],
            "input_tokens_stimati": request.input_tokens_estimate,
            "input_tokens_breakdown": request.input_tokens_breakdown,
            "input_estimate_method": request.input_estimate_method,
            "input_tokenizer_revision": request.input_tokenizer_revision,
            "fonti": [s.model_dump(mode="json", exclude={"text"}) for s in self.catalog.sources],
            "tracciabilita": {
                "schema_version": TRACE_SCHEMA_VERSION,
                "technical": "not_run",
                "clinical_review": "not_performed",
                "localizzazione": self.catalog.segmentation_version,
            },
        }
        self.finish("preparato")

    @contextmanager
    def open_draft(self, *, validation_context=nullcontext) -> Iterator[TextIO]:
        """Conserva sempre il JSON grezzo; pubblica le viste solo dopo i controlli."""
        with self.partial.open("x", encoding="utf-8") as output:
            yield output
        with validation_context():
            try:
                raw = self.partial.read_text(encoding="utf-8")
                if not raw.strip():
                    raise ValueError("Il modello non ha prodotto testo.")
                draft = parse_draft(raw, self.catalog, self.template)
                text = render_draft(draft, self.template)
                validate_draft(text, self.template)
                context = self.packet.context
                result = TraceResult(
                    binding=TraceBinding(
                        run_id=self.report["run_id"],
                        context_id=context.id,
                        dataset={"id": context.dataset, "version": context.dataset_version},
                        template={
                            "id": self.template.id,
                            "version": self.template.version,
                            "sha256": self.template.sha256,
                        },
                    ),
                    catalog=self.catalog,
                    draft=draft,
                    facts=self.facts,
                    validation={"technical": "passed", "clinical_review": "not_performed"},
                )
                views = {
                    self.structured: result.model_dump_json(indent=2) + "\n",
                    self.evidences: render_evidences(result, self.template),
                    self.draft: text,
                }
                for path, content in views.items():
                    temporary = path.with_suffix(path.suffix + ".tmp")
                    temporary.write_text(content, encoding="utf-8")
                    temporary.replace(path)
            except ValueError:
                self.report["tracciabilita"]["technical"] = "failed"
                raise
            self.report["tracciabilita"]["technical"] = "passed"

    def start(self) -> None:
        self.report["richieste_llm_avviate"] = 1
        self.finish("in_corso")

    def finish(self, status: str, *, error: BaseException | None = None, usage=None) -> None:
        self.report["stato"] = status
        if error is not None:
            self.report["errore_tipo"] = type(error).__name__
        if usage:
            self.report["consumo"] = dict(usage)
            self.report["richieste_llm_avviate"] = usage.get("metrics", {}).get(
                "model_requests_started", self.report["richieste_llm_avviate"]
            )
        if status in {"bozza_da_revisionare", "fallito", "interrotto"}:
            self.report["artefatti_sha256"] = {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in (
                    self.request,
                    self.catalog_path,
                    self.facts_path,
                    self.partial,
                    self.structured,
                    self.evidences,
                    self.draft,
                )
                if path.is_file()
            }
        path = self.directory / "esito.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
