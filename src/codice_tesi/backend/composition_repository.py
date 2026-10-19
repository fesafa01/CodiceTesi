"""Query e persistenza dei contesti documentali in PostgreSQL."""

import random

from psycopg.types.json import Jsonb

from codice_tesi.backend.database import Database
from codice_tesi.backend.repository import ClinicalError
from codice_tesi.discharge.models import CompositionPacket, Context, EvidenceSource
from codice_tesi.discharge.template import load_template


async def save_dataset(
    database: Database, contexts: list[Context], sources: list[EvidenceSource]
) -> tuple[bool, int]:
    """Importa contesti e fonti della stessa versione in una transazione. Il lock serializza
    import concorrenti; un dataset differente non sovrascrive quello già presente."""
    if not contexts:
        raise ValueError("Nessun contesto da importare.")
    context_ids = {context.id for context in contexts}
    if len(context_ids) != len(contexts) or len({source.id for source in sources}) != len(sources):
        raise ValueError("Identificativi duplicati nell'importazione.")
    datasets = {(context.dataset, context.dataset_version) for context in contexts}
    if len(datasets) != 1:
        raise ValueError("Ogni importazione deve contenere una sola versione di un dataset.")
    if any(source.context_id not in context_ids for source in sources):
        raise ValueError("Fonte senza contesto nell'importazione.")
    dataset_id, digest = datasets.pop()
    async with database.connection() as connection:
        # Serializza import concorrenti; non aggiorna o sostituisce contesti esistenti.
        await connection.execute("SELECT pg_advisory_xact_lock(183627411)")
        cursor = await connection.execute(
            "SELECT DISTINCT payload->>'dataset_version' AS version FROM composition_contexts "
            "WHERE payload->>'dataset' = %s",
            (dataset_id,),
        )
        versions = {row["version"] for row in await cursor.fetchall()}
        if versions:
            if versions == {digest}:
                return False, len(contexts)
            raise ValueError(
                "È già presente una versione differente del dataset; usare un DB separato."
            )
        async with connection.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO composition_contexts (id, payload) VALUES (%s, %s)",
                [(c.id, Jsonb(c.model_dump(mode="json"))) for c in contexts],
            )
            await cursor.executemany(
                "INSERT INTO composition_sources (id, context_id, payload) VALUES (%s, %s, %s)",
                [(s.id, s.context_id, Jsonb(s.model_dump(mode="json"))) for s in sources],
            )
    return True, len(contexts)


async def read_packet(database: Database, *, context_id=None, seed=None) -> CompositionPacket:
    """Legge contesto e note nello stesso snapshot, poi aggiunge il template Campus.

    Il seed seleziona soltanto il contesto; tutte le sue note vengono incluse.
    """
    async with database.connection(read_only=True) as connection:
        if context_id is None:
            cursor = await connection.execute("SELECT id FROM composition_contexts ORDER BY id")
            ids = [row["id"] for row in await cursor.fetchall()]
            if not ids:
                raise ClinicalError(409, "NESSUN_CONTESTO", "Importare un dataset di compilazione.")
            context_id = random.Random(seed).choice(ids)
        cursor = await connection.execute(
            "SELECT payload FROM composition_contexts WHERE id = %s", (context_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ClinicalError(404, "CONTESTO_NON_TROVATO", "Contesto non disponibile.")
        context = Context.model_validate(row["payload"])
        if context.id != context_id:
            raise ValueError("Identità del contesto incoerente con il database.")
        cursor = await connection.execute(
            "SELECT id, payload FROM composition_sources WHERE context_id = %s ORDER BY id",
            (context_id,),
        )
        sources = []
        for row in await cursor.fetchall():
            source = EvidenceSource.model_validate(row["payload"])
            if source.id != row["id"]:
                raise ValueError("Identità della fonte incoerente con il database.")
            sources.append(source)
        return CompositionPacket(context=context, sources=tuple(sources), template=load_template())
