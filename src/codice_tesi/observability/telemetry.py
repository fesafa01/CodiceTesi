"""OTLP locale: trace per esecuzione e metriche a cardinalità limitata.

Instrumentazione esplicita: nessun prompt, testo clinico, reasoning o messaggio
d'eccezione viene esportato. I provider appartengono alla singola esecuzione,
senza modificare i provider globali o duplicare gli span del framework.
"""

from contextlib import contextmanager
from time import monotonic

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NoOpTracerProvider, SpanKind, StatusCode
from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelemetryConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TELEMETRY_", env_file="observability/.env", extra="forbid"
    )
    enabled: bool = False
    endpoint: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:4318")
    project: str = "tesi-dimissioni"
    export_timeout_seconds: float = Field(default=2, gt=0, le=10)


class Telemetry:
    def __init__(self, config: TelemetryConfig):
        self.config = config
        self.provider = None
        self.meter_provider = None
        self.tracer = NoOpTracerProvider().get_tracer("codice_tesi")
        if not config.enabled:
            return
        resource = Resource.create(
            {
                "service.name": "codice-tesi",
                "service.version": "0.1.0",
                "openinference.project.name": config.project,
                "deployment.environment.name": "local",
            }
        )
        endpoint = str(config.endpoint).rstrip("/")
        self.provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        self.provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=endpoint + "/v1/traces", timeout=config.export_timeout_seconds
                ),
                schedule_delay_millis=1000,
                export_timeout_millis=int(config.export_timeout_seconds * 1000),
            )
        )
        self.tracer = self.provider.get_tracer("codice_tesi", "1.0")
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=endpoint + "/v1/metrics",
                timeout=config.export_timeout_seconds,
                # Il Collector accumula i delta tra processi diversi.
                preferred_temporality={
                    Counter: AggregationTemporality.DELTA,
                    Histogram: AggregationTemporality.DELTA,
                },
            ),
            export_interval_millis=10000,
            export_timeout_millis=int(config.export_timeout_seconds * 1000),
        )
        self.meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[reader],
            shutdown_on_exit=False,
            views=[
                View(
                    instrument_name="*",
                    instrument_unit="s",
                    aggregation=ExplicitBucketHistogramAggregation(
                        boundaries=(
                            0.001,
                            0.005,
                            0.01,
                            0.025,
                            0.05,
                            0.1,
                            0.25,
                            0.5,
                            1,
                            2.5,
                            5,
                            10,
                            20,
                            30,
                            60,
                            120,
                            240,
                            480,
                            960,
                        )
                    ),
                ),
                View(
                    instrument_name="*",
                    instrument_unit="{token}",
                    aggregation=ExplicitBucketHistogramAggregation(
                        boundaries=(
                            1,
                            4,
                            16,
                            64,
                            256,
                            1024,
                            4096,
                            16384,
                            65536,
                            262144,
                        )
                    ),
                ),
                View(
                    instrument_name="*",
                    instrument_unit="{token}/s",
                    aggregation=ExplicitBucketHistogramAggregation(
                        boundaries=(1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
                    ),
                ),
            ],
        )
        meter = self.meter_provider.get_meter("codice_tesi", "1.0")
        self.runs = meter.create_counter("tesi.executions", unit="{execution}")
        self.total = meter.create_histogram("tesi.execution.duration", unit="s")
        self.stages = meter.create_histogram("tesi.stage.duration", unit="s")
        self.agent_duration = meter.create_histogram("tesi.agent.duration", unit="s")
        self.agent_requests = meter.create_histogram("tesi.agent.requests", unit="{request}")
        self.duration = meter.create_histogram("gen_ai.client.operation.duration", unit="s")
        self.first_chunk = meter.create_histogram(
            "gen_ai.client.operation.time_to_first_chunk", unit="s"
        )
        self.first_text = meter.create_histogram("tesi.llm.time_to_first_text", unit="s")
        self.tokens = meter.create_histogram("gen_ai.client.token.usage", unit="{token}")
        self.reasoning = meter.create_histogram("tesi.llm.reasoning_tokens", unit="{token}")
        self.rate = meter.create_histogram(
            "tesi.llm.output_tokens_per_request_second", unit="{token}/s"
        )

    @contextmanager
    def span(self, name, *, attributes=None, kind=SpanKind.INTERNAL):
        with self.tracer.start_as_current_span(
            name,
            attributes=attributes,
            kind=kind,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as error:
                span.set_status(StatusCode.ERROR)
                span.set_attribute("error.type", type(error).__name__)
                raise

    @contextmanager
    def stage(self, record, name):
        started = monotonic()
        try:
            with self.span(name, attributes={"openinference.span.kind": "CHAIN"}):
                yield
        except BaseException:
            record.data["failure_stage"] = name
            raise
        finally:
            record.data["timings"][name + "_seconds"] = round(monotonic() - started, 6)

    def record_metrics(self, record):
        try:
            self._record_metrics(record)
        except Exception as error:
            record.data["telemetry_collection_error_type"] = type(error).__name__

    def _record_metrics(self, record):
        if self.meter_provider is None:
            return
        data = record.data
        attributes = {
            "tesi.project": self.config.project,
            "tesi.status": data["status"],
            "tesi.dry_run": data["dry_run"],
            "gen_ai.request.model": data["model_config"]["model"],
            "tesi.thinking": data["model_config"]["thinking"],
            "tesi.tools_enabled": data["model_config"]["tools_enabled"],
        }
        self.runs.add(1, attributes)
        self.total.record(data["timings"]["total_seconds"], attributes)
        for name, value in data["timings"].items():
            if name != "total_seconds":
                self.stages.record(value, {**attributes, "tesi.stage": name})
        llm = data.get("llm") or {}
        model_attributes = {
            **attributes,
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "local_openai_compatible",
        }
        if llm.get("cycle_duration_seconds") is not None:
            self.agent_duration.record(llm["cycle_duration_seconds"], attributes)
        if llm.get("model_requests_started") is not None:
            self.agent_requests.record(llm["model_requests_started"], attributes)
        samples = llm.get("requests", [])
        for sample in samples:
            for field, instrument in (
                ("request_duration_seconds", self.duration),
                ("time_to_first_chunk_seconds", self.first_chunk),
                ("time_to_first_text_seconds", self.first_text),
                ("output_tokens_per_request_second", self.rate),
            ):
                if sample.get(field) is not None:
                    instrument.record(sample[field], model_attributes)
            if sample.get("usage_complete") and not sample.get("usage_issues"):
                for token_type in ("input", "output"):
                    value = sample.get(token_type + "_tokens")
                    if value is not None:
                        self.tokens.record(
                            value, {**model_attributes, "gen_ai.token.type": token_type}
                        )
                if sample.get("reasoning_tokens") is not None:
                    self.reasoning.record(sample["reasoning_tokens"], model_attributes)

    def close(self):
        if self.provider is None:
            return {"enabled": False, "export_confirmed": None}
        timeout = int(self.config.export_timeout_seconds * 1000)
        # Un endpoint indisponibile non deve cambiare l'esito della generazione.
        result = {"enabled": True, "export_confirmed": None}
        try:
            traces = self.provider.force_flush(timeout_millis=timeout)
            metrics = self.meter_provider.force_flush(timeout_millis=timeout)
            result.update(
                {
                    "flush_completed": bool(traces and metrics),
                    "note": "flush_completed non attesta ingestione; verificare Collector/Phoenix",
                }
            )
        except Exception as error:
            result.update(flush_completed=False, error_type=type(error).__name__)
        finally:
            for shutdown in (
                self.provider.shutdown,
                lambda: self.meter_provider.shutdown(timeout_millis=timeout),
            ):
                try:
                    shutdown()
                except Exception as error:
                    result["shutdown_error_type"] = type(error).__name__
        return result
