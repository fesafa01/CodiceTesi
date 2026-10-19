"""Convenzioni comuni dei contratti scambiati tra backend e stesura."""

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """Rifiuta proprietà sconosciute e impedisce modifiche accidentali allo stato.

    Le collezioni sono tuple, per conservare immutabile il pacchetto
    deterministico ricevuto dal backend.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
