"""Prepara una sola richiesta con tutte le note e tutti i campi, senza troncamenti."""

from dataclasses import dataclass

from codice_tesi.config import ModelConfig
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.prompts import build_instructions, build_prompt
from codice_tesi.discharge.token_count import estimate_input_tokens


@dataclass(frozen=True)
class PreparedRequest:
    instructions: str
    prompt: str
    input_tokens_estimate: int
    input_estimate_method: str
    input_tokenizer_revision: str | None


def prepare_request(
    packet: CompositionPacket,
    config: ContextConfig,
    *,
    output_tokens: int,
    model_config: ModelConfig | None = None,
) -> PreparedRequest:
    instructions, prompt = build_instructions(packet.template), build_prompt(packet)
    token_estimate = estimate_input_tokens(instructions, prompt, model_config or ModelConfig())
    estimate = token_estimate.tokens
    if estimate + output_tokens + config.safety_margin_tokens > config.window_tokens:
        raise ValueError(
            f"Contesto troppo grande: {estimate} token stimati di input, "
            f"{output_tokens} riservati all'output, margine {config.safety_margin_tokens}; "
            f"budget {config.window_tokens}. Nessuna nota è stata troncata."
        )
    return PreparedRequest(
        instructions, prompt, estimate, token_estimate.method, token_estimate.tokenizer_revision
    )
