"""Tutte le note del contesto sono disponibili per ogni campo Campus."""

from codice_tesi.discharge.models import CompositionPacket, Context, EvidenceSource, SectionInput
from codice_tesi.discharge.template import load_template

POLICY_VERSION = "campus-context-v2"


def prepare_packet(context: Context, sources: list[EvidenceSource]) -> CompositionPacket:
    """Ordina e isola le note; la compilabilità dei campi viene valutata dall'agente."""
    if any(source.context_id != context.id for source in sources):
        raise ValueError("Fonti di contesti diversi: compilazione rifiutata.")
    if len({source.id for source in sources}) != len(sources):
        raise ValueError("Identificativi fonte duplicati.")
    template = load_template()
    notes = tuple(sorted(sources, key=lambda source: source.id))
    return CompositionPacket(
        policy_version=POLICY_VERSION,
        template=template,
        context=context,
        sections=tuple(SectionInput(section_id=s.id, sources=notes) for s in template.sections),
    )


def validate_plan(packet: CompositionPacket) -> CompositionPacket:
    """Rifiuta template alterati e campi che non ricevono tutte le note del contesto."""
    if packet.policy_version != POLICY_VERSION or packet.template != load_template():
        raise ValueError("Template o policy non aggiornati: ricostruire anche il backend.")
    registry = {source.id: source for section in packet.sections for source in section.sources}
    expected = prepare_packet(packet.context, list(registry.values()))
    if packet.sections != expected.sections or packet.excluded_source_ids:
        raise ValueError(
            "Piano delle note non coerente: ogni campo deve ricevere il contesto completo."
        )
    return packet
