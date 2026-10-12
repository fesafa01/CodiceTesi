# CodiceTesi

Repository di sviluppo della tesi di laurea magistrale di Federico Bini.
Il lavoro studia la sovranità del dato e dell'AI applicata al supporto documentale
clinico, con la lettera di dimissione come caso di studio e l'obiettivo di un
sistema eseguibile on-premise, con output verificabili e revisione professionale.

Il primo componente è un agente **PydanticAI** che invia richieste all'API
OpenAI-compatible del server `spark-413e`, raggiungibile tramite **Tailscale**.
Questa è una base sperimentale: integrazioni cliniche, strumenti dell'agente e
valutazione di fedeltà, completezza, tracciabilità e latenza saranno sviluppati
nelle fasi successive. L'accesso via Tailscale è la modalità di sviluppo remoto;
non dimostra da solo il controllo sull'intera catena di inferenza.

## Avvio

Requisiti: Python 3.13, [uv](https://docs.astral.sh/uv/) e Tailscale con accesso al server.
Eseguire dalla radice di questa repository:

```bash
uv sync --locked
uv run tesi-agent --list-models
uv run tesi-agent "Rispondi brevemente: qual è il tuo compito?"
uv run tesi-agent --stream "Rispondi soltanto: OK"
```

## Configurazione

Modello e generazione si configurano in **`model.toml`**. Sono attivi solo il modello
`deepseek-v4-flash`, il limite di 2048 token e la risposta senza streaming. I parametri
commentati non vengono inviati: il server conserva i propri default.

| Parametro | Effetto |
| --- | --- |
| `model` | ID del modello: `deepseek-v4-flash` oppure `deepseek-v4-pro`. |
| `max_tokens` | Limite dei token generati, incluso l'eventuale ragionamento. |
| `temperature` | Regola la variabilità del campionamento. |
| `top_p`, `top_k`, `min_p` | Filtri sul campionamento dei token. |
| `stop` | Lista di sequenze che interrompono la generazione. |
| `seed` | Seed richiesto al server; non garantisce risultati identici. |
| `reasoning_effort` | Livello di ragionamento; i valori accettati dipendono dal backend. |
| `stream` | Mostra il testo progressivamente; `--stream` e `--no-stream` hanno precedenza. |
| `tool_choice` | `auto`, `none` o `required`; quest'ultimo obbliga un tool nella prima chiamata. |

Il `.env`, ignorato da Git, contiene la connessione:

```dotenv
LLM_BASE_URL=http://spark-413e.tail9c981a.ts.net:8000/v1
LLM_API_KEY=not-required
LLM_TIMEOUT=120
```

Questi sono anche i valori predefiniti: `.env` serve solo per modificarli.
`not-required` è un segnaposto per l'endpoint attuale senza autenticazione.
Ogni campo può essere sovrascritto con `LLM_<NOME>`: l'ordine è argomenti Python,
variabili d'ambiente, `.env`, `model.toml`, default del codice. Per esempio:

```bash
LLM_MODEL=deepseek-v4-pro uv run tesi-agent "Rispondi soltanto: OK"
LLM_TEMPERATURE=0.2 uv run tesi-agent --no-stream "Rispondi soltanto: OK"
```

La risposta di `/v1/models` descrive capacità e limiti, non i valori in uso.
`context_length` e `top_provider.max_completion_tokens` (100000 dichiarati dal server)
non si modificano dal client; impostare `max_tokens` non aumenta questi limiti.
`supported_parameters` non specifica i default né i valori ammessi per `reasoning_effort`.
`top_k` e `min_p` vengono aggiunti al corpo JSON tramite `extra_body` del client.

## Uso da Python

La CLI è solo un'interfaccia. L'agente è utilizzabile direttamente e il client HTTP
viene chiuso automaticamente all'uscita dal contesto:

```python
from codice_tesi.agent import create_agent


async def chiedi(prompt: str) -> str:
    async with create_agent() as agent:
        return (await agent.run(prompt)).output
```

`tools` indica le funzioni disponibili all'agente, da registrare in Python con
`@agent.tool` o `@agent.tool_plain` dentro il contesto. PydanticAI ne genera lo schema,
lo invia al server ed esegue le eventuali chiamate. Non sono attivi tool predefiniti:
il flag `required` da solo non ne crea e senza tool registrati produce un errore.
Dopo la prima chiamata obbligatoria, si passa ad `auto` per consentire la risposta finale.
Per lo streaming da Python usare
`agent.run_stream()` e `response.stream_text(delta=True)`; il campo `stream` del TOML
controlla la CLI.

Ogni comando esegue una richiesta indipendente: non conserva una cronologia e non
abilita telemetria dell'agente. Per le prove iniziali usare testi sintetici.

## Sviluppo

Il codice è in `src/codice_tesi/`: `config.py` gestisce la configurazione,
`agent.py` costruisce l'agente e `cli.py` espone il comando `tesi-agent`.
Le dipendenze sono in `pyproject.toml` e bloccate in `uv.lock`, da versionare insieme.

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

I test usano un endpoint simulato e non richiedono Tailscale.
L'integrazione segue la [documentazione PydanticAI per API compatibili](https://pydantic.dev/docs/ai/models/compatible-apis/).
