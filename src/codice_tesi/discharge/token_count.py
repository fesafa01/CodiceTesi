"""Conteggio locale DeepSeek V4 per una richiesta testuale system + user, senza tool."""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer

from codice_tesi.config import ModelConfig

TOKENIZER_REVISION = "deepseek-ai/DeepSeek-V4-Flash@60d8d70770c6776ff598c94bb586a859a38244f1"
DEEPSEEK_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}
# Prefisso del template ufficiale encoding_dsv4.py, distribuito sotto licenza MIT.
MAX_EFFORT_PREFIX = (
    "Reasoning Effort: Absolute maximum with no shortcuts permitted.\n"
    "You MUST be very thorough in your thinking and comprehensively decompose the problem "
    "to resolve the root cause, rigorously stress-testing your logic against all potential "
    "paths, edge cases, and adversarial scenarios.\n"
    "Explicitly write out your entire deliberation process, documenting every intermediate "
    "step, considered alternative, and rejected hypothesis to ensure absolutely no "
    "assumption is left unchecked.\n\n"
)


@dataclass(frozen=True)
class InputTokenEstimate:
    tokens: int
    method: str
    tokenizer_revision: str | None = None


@lru_cache(maxsize=1)
def _deepseek_tokenizer() -> Tokenizer:
    # Solo file incluso nel pacchetto: nessun download o invio di testo a servizi esterni.
    path = Path(__file__).with_name("tokenizers") / "deepseek_v4.json"
    return Tokenizer.from_file(str(path))


def estimate_input_tokens(
    instructions: str, prompt: str, config: ModelConfig
) -> InputTokenEstimate:
    if config.model not in DEEPSEEK_MODELS:
        # Un tokenizer diverso non deve essere presentato come quello del modello richiesto.
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": prompt},
        ]
        estimate = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 512
        return InputTokenEstimate(estimate, "utf8_json_bytes_plus_512_not_tokenizer_count")

    thinking = config.thinking and config.reasoning_effort not in {"none", "off"}
    prefix = MAX_EFFORT_PREFIX if thinking and config.reasoning_effort == "max" else ""
    # Template DS4 per il flusso corrente: un system, un user, apertura assistant.
    # Resta una stima del prompt del runtime, coperta dal margine di ContextConfig.
    rendered = (
        "<｜begin▁of▁sentence｜>"
        + prefix
        + instructions
        + "<｜User｜>"
        + prompt
        + "<｜Assistant｜>"
        + ("<think>" if thinking else "</think>")
    )
    count = len(_deepseek_tokenizer().encode(rendered, add_special_tokens=False).ids)
    return InputTokenEstimate(count, "deepseek_v4_local_chat_template_v1", TOKENIZER_REVISION)
