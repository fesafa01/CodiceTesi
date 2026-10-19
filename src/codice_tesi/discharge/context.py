"""Prepara una sola richiesta con tutte le note e tutti i campi, senza troncamenti."""

import json
from dataclasses import dataclass

from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.policy import validate_plan
from codice_tesi.discharge.prompts import build_instructions, build_prompt


@dataclass(frozen=True)
class PreparedRequest:
    instructions: str
    prompt: str
    input_tokens_estimate: int


def prepare_request(
    packet: CompositionPacket, config: ContextConfig, *, output_tokens: int
) -> PreparedRequest:
    validate_plan(packet)
    instructions, prompt = build_instructions(packet.template), build_prompt(packet)
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
    # Stima prudenziale, non conteggio del tokenizer del server.
    estimate = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 512
    if estimate + output_tokens + config.safety_margin_tokens > config.window_tokens:
        raise ValueError(
            f"Contesto troppo grande: {estimate} token stimati di input, "
            f"{output_tokens} riservati all'output, margine {config.safety_margin_tokens}; "
            f"budget {config.window_tokens}. Nessuna nota è stata troncata."
        )
    return PreparedRequest(instructions, prompt, estimate)
