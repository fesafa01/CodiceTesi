"""Misure sui chunk del provider, senza conservare testo o reasoning.

I due hook PydanticAI usati dall'adattatore sono documentati per l'estensione
dei provider. Un test di protocollo ne verifica il comportamento sul lockfile.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic


def _count(value):
    return value if type(value) is int and value >= 0 else None


@dataclass
class StreamObservation:
    started: float | None = None
    ended: float | None = None
    first_chunk: float | None = None
    first_content: float | None = None
    first_text: float | None = None
    first_reasoning: float | None = None
    last_content: float | None = None
    last_chunk: float | None = None
    chunks: int = 0
    content_chunks: int = 0
    gap_sum: float = 0
    gap_max: float = 0
    usage: dict | None = None
    response_model: str | None = None
    response_id: str | None = None
    server_timings: dict = field(default_factory=dict)

    def start(self):
        self.started = monotonic()

    def observe(self, chunk):
        now = monotonic()
        self.chunks += 1
        if self.first_chunk is None:
            self.first_chunk = now
        self.last_chunk = now
        self.response_model = chunk.model or self.response_model
        self.response_id = chunk.id or self.response_id
        if chunk.usage is not None:
            # Usage nello stream è uno snapshot cumulativo: mai sommare i chunk.
            self.usage = chunk.usage.model_dump(exclude_none=True)
        timings = (chunk.model_extra or {}).get("timings")
        if isinstance(timings, dict):
            self.server_timings.update(
                {
                    k: v
                    for k, v in timings.items()
                    if k in {"prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "cache_n"}
                    and type(v) in (int, float)
                    and v >= 0
                }
            )
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            return
        text = delta.content or ""
        extras = delta.model_extra or {}
        reasoning = extras.get("reasoning_content") or extras.get("reasoning") or ""
        if isinstance(text, str) and text.strip() and self.first_text is None:
            self.first_text = now
        if isinstance(reasoning, str) and reasoning.strip() and self.first_reasoning is None:
            self.first_reasoning = now
        if text or reasoning:
            if self.first_content is None:
                self.first_content = now
            if self.last_content is not None:
                gap = now - self.last_content
                self.gap_sum += gap
                self.gap_max = max(self.gap_max, gap)
            self.last_content = now
            self.content_chunks += 1

    def snapshot(self, *, complete: bool):
        def since_start(timestamp):
            if timestamp is None or self.started is None:
                return None
            return round(timestamp - self.started, 6)

        raw = self.usage or {}
        input_tokens = _count(raw.get("prompt_tokens"))
        output_tokens = _count(raw.get("completion_tokens"))
        details = raw.get("completion_tokens_details") or {}
        reasoning = _count(details.get("reasoning_tokens"))
        cached = _count((raw.get("prompt_tokens_details") or {}).get("cached_tokens"))
        visible = None
        issues = []
        if reasoning is not None and output_tokens is not None:
            if reasoning <= output_tokens:
                visible = output_tokens - reasoning
            else:
                issues.append("reasoning_exceeds_output")
        reported_total = _count(raw.get("total_tokens"))
        calculated_total = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        if reported_total is not None and calculated_total is not None:
            if reported_total != calculated_total:
                issues.append("total_tokens_mismatch")
        duration = since_start(self.ended)
        return {
            "measurement_boundary": "provider_request_stream_client",
            "model_requests_started": int(self.started is not None),
            "response_model": self.response_model,
            "provider_response_id": self.response_id,
            "request_duration_seconds": duration,
            "time_to_first_chunk_seconds": since_start(self.first_chunk),
            "time_to_first_content_seconds": since_start(self.first_content),
            "time_to_first_text_seconds": since_start(self.first_text),
            "time_to_first_reasoning_seconds": since_start(self.first_reasoning),
            "time_to_last_content_seconds": since_start(self.last_content),
            "chunks_received": self.chunks,
            "content_chunks_received": self.content_chunks,
            "content_chunk_gap_mean_seconds": (
                round(self.gap_sum / (self.content_chunks - 1), 6)
                if self.content_chunks > 1
                else None
            ),
            "content_chunk_gap_max_seconds": (
                round(self.gap_max, 6) if self.content_chunks > 1 else None
            ),
            "usage_available": self.usage is not None,
            "usage_complete": self.usage is not None and complete,
            "usage_source": "provider_stream_usage" if self.usage is not None else None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning,
            "visible_output_tokens": visible,
            "visible_output_tokens_source": (
                "completion_tokens_minus_reasoning_tokens" if visible is not None else None
            ),
            "token_contract": "chat_completions_output_includes_reasoning",
            "cached_input_tokens": cached,
            "total_tokens": reported_total,
            "calculated_total_tokens": calculated_total,
            "usage_issues": issues,
            "output_tokens_per_request_second": (
                round(output_tokens / duration, 6)
                if complete and output_tokens is not None and duration and not issues
                else None
            ),
            "server_reported_timings": dict(self.server_timings),
            # Il client non osserva coda/prefill/decode e non li ricava dai chunk.
            "server_ttft_seconds": None,
            "server_decode_tokens_per_second": (
                round(
                    self.server_timings["predicted_n"] * 1000 / self.server_timings["predicted_ms"],
                    6,
                )
                if self.server_timings.get("predicted_ms", 0) > 0
                and "predicted_n" in self.server_timings
                else None
            ),
        }


active_observation: ContextVar[StreamObservation | None] = ContextVar(
    "discharge_stream_observation", default=None
)
