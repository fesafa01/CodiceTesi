"""Client del backend clinico, utilizzato dall'applicazione prima della stesura."""

import asyncio
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from codice_tesi.discharge.models import CompositionPacket


class PacketClient:
    """Recupera e valida il pacchetto prima della generazione."""

    def __init__(self, base_url: str, *, timeout: float = 20, token: str | None = None):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout
        self._token = token if token is not None else os.getenv("APP_MIMIC_TOKEN")

    async def fetch(self, *, seed: int, context_id: str | None) -> CompositionPacket:
        """Esegue la lettura HTTP bloccante in un thread."""
        return await asyncio.to_thread(self._fetch, seed=seed, context_id=context_id)

    def _fetch(self, *, seed: int, context_id: str | None) -> CompositionPacket:
        """Sceglie l'endpoint e rifiuta un contesto diverso da quello richiesto."""
        path = (
            f"/contesti/{quote(context_id, safe='')}/pacchetto"
            if context_id
            else "/avvio-test?" + urlencode({"seed": seed})
        )
        request = Request(self.base_url + "/api/v1/compilazione" + path)
        if self._token:
            request.add_header("Authorization", "Bearer " + self._token)
        try:
            with urlopen(request, timeout=self.timeout) as reply:
                packet = CompositionPacket.model_validate_json(reply.read())
        except HTTPError as exc:
            if exc.code == 401:
                raise ValueError(
                    "Backend HTTP 401: credenziale locale assente o non valida; "
                    "verificare APP_MIMIC_TOKEN."
                ) from None
            if exc.code == 403:
                raise ValueError(
                    "Backend HTTP 403: contesto fuori dal perimetro dell'identità locale."
                ) from None
            raise ValueError(
                f"Backend HTTP {exc.code}: verificare contesto e importazione del dataset."
            ) from None
        except URLError:
            raise ValueError("Backend non raggiungibile: verificare APP_BACKEND_URL.") from None
        if context_id is not None and packet.context.id != context_id:
            raise ValueError("Il backend ha restituito un contesto diverso da quello richiesto.")
        return packet
