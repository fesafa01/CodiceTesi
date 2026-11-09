"""Convenzioni comuni dei contratti scambiati tra backend e stesura."""

from pydantic import BaseModel, ConfigDict

TOOL_RESULTS_FILE = "risultati_tool.json"


class DraftContractError(ValueError):
    """Violazione tecnica della bozza con un messaggio privo di contenuti clinici."""


class Contract(BaseModel):
    """Rifiuta proprietà sconosciute e impedisce modifiche accidentali allo stato.

    Le collezioni sono tuple, per conservare immutabile il pacchetto
    deterministico ricevuto dal backend.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
