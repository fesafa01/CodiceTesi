"""Impostazioni della stesura, separate da connessione e parametri del modello."""

from pydantic import Field

from codice_tesi.discharge.contracts import Contract


class ContextConfig(Contract):
    """Un eventuale tetto esplicito per i chiamanti che conoscono la finestra del serving."""

    window_tokens: int | None = Field(default=None, gt=0)
    safety_margin_tokens: int = Field(default=1024, ge=0)
