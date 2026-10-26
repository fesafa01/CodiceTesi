"""Identità e assegnazioni fittizie per la sola dimostrazione locale su Synthea.

I token sono pubblici e riproducibili: simulano il confine applicativo, senza
integrazione con identità ospedaliere, login o servizi esterni.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DemoIdentity:
    subject: str
    token: str = field(repr=False)
    context_ids: frozenset[str]


SYNTHETIC_CONTEXTS = tuple(f"synthea-caso-{number:02}" for number in range(1, 31))
DEFAULT_DEMO_TOKEN = "demo-revisore"
DEMO_IDENTITIES = (
    DemoIdentity("medico-a", "demo-medico-a", frozenset(SYNTHETIC_CONTEXTS[::2])),
    DemoIdentity("medico-b", "demo-medico-b", frozenset(SYNTHETIC_CONTEXTS[1::2])),
    DemoIdentity("revisore", DEFAULT_DEMO_TOKEN, frozenset(SYNTHETIC_CONTEXTS)),
)
