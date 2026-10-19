"""Una richiesta con note e campi Campus; stream nel terminale e nello stesso TXT."""

import asyncio
import secrets
from contextlib import aclosing
from pathlib import Path

from codice_tesi import api
from codice_tesi.config import ModelConfig, Settings
from codice_tesi.discharge.artifacts import RunArtifacts
from codice_tesi.discharge.client import PacketClient
from codice_tesi.discharge.config import ContextConfig
from codice_tesi.discharge.context import prepare_request
from codice_tesi.discharge.generator import LLMDraftGenerator


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
) -> Path:
    selection_seed = seed if seed is not None else secrets.randbits(63)
    packet = await PacketClient(backend_url).fetch(seed=selection_seed, context_id=context_id)
    config = config or ModelConfig()
    request = prepare_request(
        packet, context_config or ContextConfig(), output_tokens=config.max_tokens
    )
    artifacts = RunArtifacts(
        output_dir,
        packet,
        request,
        seed=selection_seed,
        fixed_context=context_id is not None,
        config=config,
        preview=dry_run,
    )
    print(f"Contesto: {packet.context.id} ({packet.context.scope})", flush=True)
    print(f"Cartella: {artifacts.directory}", flush=True)
    print(
        f"Note nel contesto: {len(packet.sources)}; "
        f"campi Campus: {len(packet.template.sections)}; richieste LLM: 1",
        flush=True,
    )
    if dry_run:
        print(f"Richiesta pronta: {artifacts.request}", flush=True)
        return artifacts.directory

    print("Redazione in streaming. Ctrl+C conserva il testo parziale.\n", flush=True)
    generator = None
    try:
        with artifacts.open_draft() as output:
            async with api.create_client(settings or Settings(), streaming=True) as client:
                generator = LLMDraftGenerator(config, client)
                artifacts.start()
                async with aclosing(generator.stream(request)) as chunks:
                    async for chunk in chunks:
                        output.write(chunk)
                        output.flush()
                        print(chunk, end="", flush=True)
        artifacts.finish("bozza_da_revisionare", usage=generator.usage)
    except BaseException as exc:
        artifacts.finish(
            "interrotto"
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
            else "fallito",
            error=exc,
            usage=generator.usage if generator else None,
        )
        print(f"\nRedazione non completata. Testo parziale: {artifacts.partial}", flush=True)
        raise
    print(f"\n\nBozza salvata: {artifacts.draft}", flush=True)
    return artifacts.directory
