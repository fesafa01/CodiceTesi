"""Prepara la richiesta con il catalogo completo e le stime per il registro."""

from dataclasses import dataclass

from codice_tesi.config import ModelConfig
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.facts import FactInventory, extract_facts
from codice_tesi.discharge.models import MIMIC_DATASET, CompositionPacket
from codice_tesi.discharge.prompts import (
    MIMIC_PROMPT_VERSION,
    MIMIC_TOOL_PROMPT_VERSION,
    PROMPT_VERSION,
    build_instructions,
    build_prompt,
    with_read_tools,
)
from codice_tesi.discharge.provenance import EvidenceCatalog, build_catalog
from codice_tesi.discharge.token_count import estimate_input_tokens, input_token_breakdown


@dataclass(frozen=True)
class PreparedRequest:
    instructions: str
    prompt: str
    prompt_version: str
    catalog: EvidenceCatalog
    input_tokens_estimate: int
    input_estimate_method: str
    input_tokenizer_revision: str | None
    facts: FactInventory
    window_tokens: int | None
    safety_margin_tokens: int
    input_tokens_breakdown: dict[str, int]


def prepare_request(
    packet: CompositionPacket,
    config: ContextConfig,
    *,
    output_tokens: int,
    model_config: ModelConfig | None = None,
    catalog: EvidenceCatalog | None = None,
) -> PreparedRequest:
    current = build_catalog(packet)
    catalog = catalog or current
    if catalog.sources != current.sources:
        raise ValueError("Il catalogo preparato non corrisponde alle fonti del pacchetto.")
    model_config = model_config or ModelConfig()
    dataset = packet.context.dataset
    facts = extract_facts(catalog, dataset=dataset)
    instructions = build_instructions(
        packet.template,
        dataset=dataset,
        has_icu_sources=any((source.origin or "").startswith("icu/") for source in catalog.sources),
    )
    if model_config.tools_enabled:
        instructions = with_read_tools(instructions, dataset=dataset)
    prompt = build_prompt(packet, catalog, facts)
    token_estimate = estimate_input_tokens(instructions, prompt, model_config)
    estimate = token_estimate.tokens
    tool_reserve = 8192 if model_config.tools_enabled else 0
    if config.window_tokens is not None and (
        estimate + output_tokens + config.safety_margin_tokens + tool_reserve > config.window_tokens
    ):
        raise ValueError(
            f"Contesto troppo grande: {estimate} token stimati di input, "
            f"{output_tokens} riservati all'output, margine {config.safety_margin_tokens}; "
            f"riserva tool {tool_reserve}; budget {config.window_tokens}. "
            "Nessuna nota è stata troncata."
        )
    return PreparedRequest(
        instructions=instructions,
        prompt=prompt,
        prompt_version=(
            MIMIC_TOOL_PROMPT_VERSION
            if dataset == MIMIC_DATASET and model_config.tools_enabled
            else MIMIC_PROMPT_VERSION
            if dataset == MIMIC_DATASET
            else PROMPT_VERSION
        ),
        catalog=catalog,
        input_tokens_estimate=estimate,
        input_estimate_method=token_estimate.method,
        input_tokenizer_revision=token_estimate.tokenizer_revision,
        facts=facts,
        window_tokens=config.window_tokens,
        safety_margin_tokens=config.safety_margin_tokens,
        input_tokens_breakdown=input_token_breakdown(
            instructions,
            prompt,
            (source.text for source in catalog.sources),
            model_config,
            estimate,
        ),
    )
