"""Scadenza complessiva indipendente dagli eventi di ragionamento o di streaming."""

import asyncio
from contextlib import asynccontextmanager


class ExecutionTimeoutError(ValueError):
    pass


class ModelOutputLimitError(ValueError):
    def __init__(self):
        super().__init__("Il modello ha raggiunto il limite di token in uscita.")


class ToolCallValidationError(ValueError):
    """Messaggio sicuro: non contiene gli argomenti emessi dal modello."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(
            f"Chiamata a {tool_name} rifiutata: nome o parametri non validi "
            "con esaurimento delle correzioni consentite."
        )


class FinalPhaseToolCallError(ValueError):
    """Il budget ammette soltanto la risposta finale, senza altre operazioni."""

    code = "final_phase"

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool {tool_name} non eseguibile nella fase finale.")


class EvidenceReadError(ValueError):
    """Errore documentale atteso, con codice e messaggio privi di input del modello."""

    MESSAGES = {
        "call_limit": "Limite dei tentativi di lettura raggiunto.",
        "invalid_ids": "Richiedere da 1 a 10 evidenze distinte.",
        "invalid_type": "Tipo non riconosciuto: usare un tipo di evento esposto dal tool.",
        "invalid_date": "Data richiesta non valida: usare AAAA-MM-GG.",
        "inverted_dates": "Intervallo temporale invertito.",
        "invalid_limit": "Limit deve essere fra 1 e 10 e offset fra 0 e 10000.",
        "unknown_evidence": "Evidenza assente dallo snapshot autorizzato.",
        "result_budget": (
            "Risposta oltre il budget; restringere etichetta, date o numero di passaggi."
        ),
    }

    def __init__(self, code: str):
        self.code = code if code in self.MESSAGES else "read_failed"
        super().__init__(self.MESSAGES.get(self.code, "Lettura documentale non completata."))


class ModelInactivityError(ValueError):
    """Il modello non ha prodotto progressi prima del testo entro l'intervallo ammesso."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        super().__init__(
            f"Nessun progresso dal modello per {seconds:g} secondi prima della lettera. "
            "Limite di attesa iniziale/inattività raggiunto; "
            "richiesta interrotta senza retry. Contatori conservati in esito.json."
        )


@asynccontextmanager
async def execution_deadline(seconds: float | None):
    if seconds is None:
        yield
        return
    timer = asyncio.timeout(seconds)
    try:
        async with timer:
            yield
    except TimeoutError:
        if timer.expired():
            raise ExecutionTimeoutError(
                f"Durata massima di {seconds:g} secondi raggiunta; lavoro interrotto. "
                "Il rilascio delle risorse del serving va verificato separatamente."
            ) from None
        raise
