"""Impostazioni della stesura, separate da connessione e parametri del modello."""

from pydantic import Field

from codice_tesi.discharge.contracts import Contract


class ContextConfig(Contract):
    # Budget applicativo prudenziale; non è una capacità dichiarata dal server.
    """Budget applicativo della richiesta, distinto dalla capacità effettiva del server. Il
    margine copre differenze di tokenizzazione e framing; non autorizza troncamenti."""

    window_tokens: int = Field(default=100000, gt=0)
    safety_margin_tokens: int = Field(default=1024, ge=0)
