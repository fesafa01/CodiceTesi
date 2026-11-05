"""Stesura con tool opzionali, durata libera e telemetria priva di contenuti."""

import asyncio
import json
import os
import secrets
from collections.abc import Callable
from contextlib import aclosing
from hashlib import sha256
from pathlib import Path

from codice_tesi import api
from codice_tesi.config import SPARK_LLM_URL, ModelConfig, Settings
from codice_tesi.discharge.artifacts import RunArtifacts
from codice_tesi.discharge.client import PacketClient
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.generator import LLMDraftGenerator
from codice_tesi.discharge.limits import execution_deadline
from codice_tesi.discharge.models import MIMIC_DATASET, CompositionPacket
from codice_tesi.discharge.provenance import EvidenceCatalog
from codice_tesi.observability.execution import ExecutionRecord, ExperimentConfig
from codice_tesi.observability.model_spans import agent_attributes, prompt_attributes
from codice_tesi.observability.telemetry import Telemetry, TelemetryConfig


async def _record_progress(generator, artifacts, record, notify):
    while True:
        await asyncio.sleep(1)
        progress = dict(generator.progress)
        artifacts.finish("in_corso", usage={"stream": progress})
        record.data["stream_progress"] = progress
        record.save()
        notify(generator.progress.get("fase", "generazione"), progress=progress)


async def run_application(
    *,
    backend_url: str,
    packet: CompositionPacket | None = None,
    output_dir: Path,
    seed: int | None = None,
    context_id: str | None = None,
    dry_run: bool = False,
    console: bool = True,
    settings: Settings | None = None,
    config: ModelConfig | None = None,
    context_config: ContextConfig | None = None,
    experiment: ExperimentConfig | None = None,
    telemetry_config: TelemetryConfig | None = None,
    on_progress: Callable[[dict], None] | None = None,
    on_text: Callable[[str, bool], None] | None = None,
    catalog: EvidenceCatalog | None = None,
) -> Path:
    def emit(*args, **kwargs):
        if console:
            print(*args, **kwargs)

    if packet is not None:
        if context_id is not None and context_id != packet.context.id:
            raise ValueError("Il pacchetto preparato appartiene a un altro contesto.")
        context_id = packet.context.id
    config = config or ModelConfig()
    context_config = context_config or ContextConfig()
    if os.getenv("APP_MIMIC_TOKEN") or (packet and packet.context.dataset == MIMIC_DATASET):
        telemetry_config = TelemetryConfig(enabled=False)
    telemetry_config = telemetry_config or TelemetryConfig()
    record = ExecutionRecord(
        output_dir,
        experiment=experiment or ExperimentConfig(),
        config=config,
        context_id=context_id,
        dry_run=dry_run,
    )
    telemetry = Telemetry(telemetry_config)

    def notify(phase, *, progress=None):
        if on_progress:
            on_progress(
                {"run_id": record.data["run_id"], "phase": phase, "progress": progress or {}}
            )

    artifacts = generator = None
    try:
        with telemetry.span(
            "discharge.execution",
            attributes={
                "openinference.span.kind": "CHAIN",
                "tesi.run_id": record.data["run_id"],
                "tesi.experiment_id": record.data["experiment"]["experiment_id"],
                "tesi.dry_run": dry_run,
                "tesi.thinking": config.thinking,
                "tesi.tools_enabled": config.tools_enabled,
                "metadata": json.dumps(
                    {
                        "experiment": record.data["experiment"],
                        "model_config": record.data["model_config"],
                        "source_sha256": record.data["software"]["source_sha256"],
                    },
                    sort_keys=True,
                ),
            },
        ) as root:
            trace_context = root.get_span_context()
            if trace_context.is_valid:
                record.data["trace_id"] = f"{trace_context.trace_id:032x}"
                record.data["root_span_id"] = f"{trace_context.span_id:016x}"
            record.save()
            try:
                async with execution_deadline(config.total_timeout):
                    notify("preparazione")
                    selection_seed = seed if seed is not None else secrets.randbits(63)
                    record.data["selection_seed"] = selection_seed if context_id is None else None
                    with telemetry.stage(record, "fetch_packet"):
                        if packet is None:
                            packet = await PacketClient(backend_url).fetch(
                                seed=selection_seed, context_id=context_id
                            )
                    with telemetry.stage(record, "prepare_request"):
                        request = prepare_request(
                            packet,
                            context_config,
                            output_tokens=config.max_tokens,
                            model_config=config,
                            catalog=catalog,
                        )
                    with telemetry.stage(record, "save_prepared_request"):
                        artifacts = RunArtifacts(
                            output_dir,
                            packet,
                            request,
                            seed=selection_seed,
                            fixed_context=context_id is not None,
                            config=config,
                            preview=dry_run,
                        )
                        record.bind(packet, request, artifacts, context_config)
                    root.set_attribute("tesi.request_sha256", record.data["request_sha256"])
                    root.set_attribute("tesi.dataset_version", packet.context.dataset_version)
                    emit(f"Contesto: {packet.context.id} ({packet.context.scope})", flush=True)
                    emit(f"Cartella: {artifacts.directory}", flush=True)
                    emit(f"Esecuzione: {record.data['run_id']}", flush=True)
                    emit(
                        f"Note nel contesto: {len(packet.sources)}; "
                        f"campi Campus: {len(packet.template.sections)}; "
                        "richieste LLM massime: "
                        f"{config.max_requests if config.tools_enabled else 1}; "
                        f"thinking={'attivo' if config.thinking else 'disattivo'}",
                        flush=True,
                    )
                    if dry_run:
                        emit(f"Richiesta pronta: {artifacts.request}", flush=True)
                        record.finish("prepared")
                        return artifacts.directory
                    model_settings = settings or Settings()
                    if (
                        packet.context.dataset == MIMIC_DATASET
                        and str(model_settings.base_url).rstrip("/") != SPARK_LLM_URL
                        and model_settings.base_url.host not in {"127.0.0.1", "[::1]", "localhost"}
                    ):
                        raise ValueError(
                            "Per MIMIC usare un serving locale o lo Spark configurato."
                        )
                    emit(
                        "Redazione delle sezioni e citazioni in JSON. "
                        "Ctrl+C conserva la risposta parziale.\n",
                        flush=True,
                    )
                    record.data["status"] = "running"
                    record.save()
                    with artifacts.open_draft(
                        validation_context=lambda: telemetry.stage(record, "validate_and_save")
                    ) as output:
                        async with api.create_client(model_settings) as client:
                            generator = LLMDraftGenerator(config, client)
                            generator.telemetry = telemetry
                            if config.tools_enabled:

                                def save_partial(text, reset):
                                    if reset:
                                        output.seek(0)
                                        output.truncate()
                                    output.write(text)
                                    output.flush()
                                    if on_text:
                                        on_text(text, reset)

                                generator.partial_callback = save_partial
                            artifacts.start()
                            progress_task = asyncio.create_task(
                                _record_progress(generator, artifacts, record, notify)
                            )
                            try:
                                with telemetry.stage(record, "generate_draft"):
                                    with telemetry.span(
                                        "discharge.agent_run",
                                        attributes=prompt_attributes(request),
                                    ) as agent_span:
                                        try:
                                            async with aclosing(
                                                generator.stream(request)
                                            ) as chunks:
                                                async for chunk in chunks:
                                                    if config.tools_enabled:
                                                        output.seek(0)
                                                        output.truncate()
                                                    output.write(chunk)
                                                    output.flush()
                                                    if on_text:
                                                        on_text(chunk, config.tools_enabled)
                                                    if packet.context.dataset != MIMIC_DATASET:
                                                        emit(chunk, end="", flush=True)
                                        finally:
                                            metrics = generator.usage.get("metrics", {})
                                            agent_span.set_attributes(
                                                agent_attributes(
                                                    metrics, generator.usage.get("tools", {})
                                                )
                                            )
                            finally:
                                progress_task.cancel()
                                try:
                                    await progress_task
                                except asyncio.CancelledError:
                                    pass
                    notify("controllo_risultato")
                    artifacts.finish("bozza_da_revisionare", usage=generator.usage)
                    record.data["draft_sha256"] = sha256(artifacts.draft.read_bytes()).hexdigest()
                    record.data["artifact_hashes"] = dict(artifacts.report["artefatti_sha256"])
                    record.finish("completed", llm=generator.usage.get("metrics"))
                    emit(f"\n\nBozza salvata: {artifacts.draft}", flush=True)
                    emit(f"Fonti per sezione: {artifacts.evidences}", flush=True)
                    return artifacts.directory
            except BaseException as exc:
                interrupted = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
                record.finish(
                    "interrupted" if interrupted else "failed",
                    error=exc,
                    llm=generator.usage.get("metrics") if generator else None,
                )
                if artifacts is not None:
                    if (
                        generator
                        and getattr(generator, "partial_text", "")
                        and config.tools_enabled
                    ):
                        artifacts.partial.write_text(generator.partial_text, encoding="utf-8")
                    artifacts.finish(
                        "interrotto" if interrupted else "fallito",
                        error=exc,
                        usage=generator.usage if generator else None,
                    )
                    emit(
                        f"\nRedazione non completata. Testo parziale: {artifacts.partial}",
                        flush=True,
                    )
                raise
            finally:
                root.set_attribute("tesi.status", record.data["status"])
                telemetry.record_metrics(record)
    finally:
        record.data["telemetry"] = telemetry.close()
        record.save()
