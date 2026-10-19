"""Client del backend clinico, utilizzato dall'applicazione prima della stesura."""

import asyncio
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from codice_tesi.discharge.models import CompositionPacket
from codice_tesi.discharge.policy import validate_plan


class PacketClient:
    """Client riservato all’orchestratore. Recupera un solo snapshot e verifica che caso e
    template siano quelli attesi prima di introdurli nella memoria di stesura."""

    def __init__(self, base_url: str, *, timeout: float = 20):
        """Imposta URL e timeout del backend clinico; non riceve credenziali SQL o
        riferimenti al generatore."""
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    async def fetch(self, *, seed: int, context_id: str | None) -> CompositionPacket:
        """Esegue la lettura bloccante HTTP in un thread, mantenendo libera la coroutine
        applicativa."""
        return await asyncio.to_thread(self._fetch, seed=seed, context_id=context_id)

    def _fetch(self, *, seed: int, context_id: str | None) -> CompositionPacket:
        """Sceglie l’endpoint in base al contesto fissato o al seed e valida il pacchetto. Un
        cambio di identità o template viene rifiutato, non corretto dal modello."""
        path = (
            f"/contesti/{quote(context_id, safe='')}/pacchetto"
            if context_id
            else "/avvio-test?" + urlencode({"seed": seed})
        )
        try:
            with urlopen(
                self.base_url + "/api/v1/compilazione" + path, timeout=self.timeout
            ) as reply:
                packet = validate_plan(CompositionPacket.model_validate_json(reply.read()))
        except HTTPError as exc:
            raise ValueError(
                f"Backend HTTP {exc.code}: verificare contesto e importazione con import-ecream."
            ) from None
        except URLError:
            raise ValueError("Backend non raggiungibile: verificare APP_BACKEND_URL.") from None
        if context_id is not None and packet.context.id != context_id:
            raise ValueError("Il backend ha restituito un contesto diverso da quello richiesto.")
        return packet
