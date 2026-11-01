"""Una richiesta di stesura con registro durevole e telemetria priva di contenuti."""

import asyncio
import json
import secrets
from contextlib import aclosing
from hashlib import sha256
from pathlib import Path

from opentelemetry.trace import SpanKind

from codice_tesi import api
from codice_tesi.config import ModelConfig, Settings
from codice_tesi.discharge.artifacts import RunArtifacts
from codice_tesi.discharge.client import PacketClient
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.generator import LLMDraftGenerator
from codice_tesi.observability.execution import ExecutionRecord, ExperimentConfig
from codice_tesi.observability.hardware import HardwareConfig, HardwareObservation
from codice_tesi.observability.telemetry import Telemetry, TelemetryConfig


async def _record_progress(generator, artifacts, record, hardware):
    while True:
        await asyncio.sleep(15)
        progress = dict(generator.progress)
        artifacts.finish("in_corso", usage={"stream": progress})
        record.data["stream_progress"] = progress
        record.data["hardware"] = hardware.summary()
        record.save()


async def run_application(
    *,
    backend_url: str,
    output_dir: Path,
    seed: int | None = None,
    context_id: str | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    config: ModelConfig | None = None,
    context_config: ContextConfig | None = None,
    experiment: ExperimentConfig | None = None,
    telemetry_config: TelemetryConfig | None = None,
    hardware_config: HardwareConfig | None = None,
) -> Path:
    config = config or ModelConfig()
    context_config = context_config or ContextConfig()
    telemetry_config = telemetry_config or TelemetryConfig()
    record = ExecutionRecord(
        output_dir,
        experiment=experiment or ExperimentConfig(),
        config=config,
        context_id=context_id,
        dry_run=dry_run,
    )
    telemetry = Telemetry(telemetry_config)
    hardware = HardwareObservation(
        hardware_config or HardwareConfig(), output_dir, record.data["run_id"]
    )
    record.data["hardware"] = hardware.summary()
    artifacts = generator = None
    try:
        with telemetry.span(
            "discharge.execution",
            attributes={
                "openinference.span.kind": "CHAIN",
                "tesi.run_id": record.data["run_id"],
                "tesi.experiment_id": record.data["experiment"]["experiment_id"],
                "tesi.dry_run": dry_run,
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
                selection_seed = seed if seed is not None else secrets.randbits(63)
                record.data["selection_seed"] = selection_seed if context_id is None else None
                with telemetry.stage(record, "fetch_packet"):
                    packet = await PacketClient(backend_url).fetch(
                        seed=selection_seed, context_id=context_id
                    )
                with telemetry.stage(record, "prepare_request"):
                    request = prepare_request(
                        packet,
                        context_config,
                        output_tokens=config.max_tokens,
                        model_config=config,
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
                print(f"Contesto: {packet.context.id} ({packet.context.scope})", flush=True)
                print(f"Cartella: {artifacts.directory}", flush=True)
                print(f"Esecuzione: {record.data['run_id']}", flush=True)
                print(
                    f"Note nel contesto: {len(packet.sources)}; "
                    f"campi Campus: {len(packet.template.sections)}; richieste LLM: 1; "
                    f"thinking={'attivo' if config.thinking else 'disattivo'}",
                    flush=True,
                )
                if dry_run:
                    print(f"Richiesta pronta: {artifacts.request}", flush=True)
                    record.finish("prepared")
                    return artifacts.directory
                print("Redazione in streaming. Ctrl+C conserva il testo parziale.\n", flush=True)
                record.data["status"] = "running"
                record.save()
                with artifacts.open_draft(
                    validation_context=lambda: telemetry.stage(record, "validate_and_save")
                ) as output:
                    async with api.create_client(settings or Settings(), streaming=True) as client:
                        generator = LLMDraftGenerator(config, client)
                        artifacts.start()
                        progress_task = asyncio.create_task(
                            _record_progress(generator, artifacts, record, hardware)
                        )
                        try:
                            with telemetry.stage(record, "generate_draft"):
                                with telemetry.span(
                                    "chat " + config.model,
                                    kind=SpanKind.CLIENT,
                                    attributes={
                                        "openinference.span.kind": "LLM",
                                        "gen_ai.operation.name": "chat",
                                        "gen_ai.request.model": config.model,
                                        "llm.model_name": config.model,
                                        "llm.invocation_parameters": json.dumps(
                                            config.request_settings(), sort_keys=True
                                        ),
                                    },
                                ) as llm_span:
                                    try:
                                        hardware.start()
                                        async with aclosing(generator.stream(request)) as chunks:
                                            async for chunk in chunks:
                                                output.write(chunk)
                                                output.flush()
                                                print(chunk, end="", flush=True)
                                    finally:
                                        await hardware.stop()
                                        record.data["hardware"] = hardware.summary()
                                        llm_span.set_attributes(hardware.span_attributes())
                                        metrics = generator.usage.get("metrics", {})
                                        llm_span.set_attributes(
                                            telemetry.llm_attributes(record, metrics)
                                        )
                        finally:
                            progress_task.cancel()
                            try:
                                await progress_task
                            except asyncio.CancelledError:
                                pass
                artifacts.finish("bozza_da_revisionare", usage=generator.usage)
                record.data["draft_sha256"] = sha256(artifacts.draft.read_bytes()).hexdigest()
                record.finish("completed", llm=generator.usage.get("metrics"))
                print(f"\n\nBozza salvata: {artifacts.draft}", flush=True)
                return artifacts.directory
            except BaseException as exc:
                interrupted = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
                record.finish(
                    "interrupted" if interrupted else "failed",
                    error=exc,
                    llm=generator.usage.get("metrics") if generator else None,
                )
                if artifacts is not None:
                    artifacts.finish(
                        "interrotto" if interrupted else "fallito",
                        error=exc,
                        usage=generator.usage if generator else None,
                    )
                    print(
                        f"\nRedazione non completata. Testo parziale: {artifacts.partial}",
                        flush=True,
                    )
                raise
            finally:
                root.set_attribute("tesi.status", record.data["status"])
                root.set_attributes(hardware.span_attributes())
                telemetry.record_metrics(record)
    finally:
        record.data["telemetry"] = telemetry.close()
        record.save()
