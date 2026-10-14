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

## Avvio con un solo comando

Dalla cartella `CodiceTesi/`, con Docker Desktop o Docker Engine e Compose attivi:

```bash
docker compose run --build --rm app
```

Il comando costruisce l'immagine e mostra i comandi disponibili. Il container
termina dopo l'esecuzione. Tutte le operazioni passano da `main.py`, che delega
a `src/codice_tesi/main.py`:

```bash
docker compose run --build --rm app modelli
docker compose run --build --rm app agente "Rispondi soltanto: OK" --no-stream
```

`Dockerfile` produce una sola immagine applicativa, con Python 3.13, dipendenze
bloccate in `uv.lock` e codice. Il container usa un utente senza privilegi.

Il file `.env` è facoltativo e viene passato al container da Compose; credenziali,
`.venv` e Git sono esclusi dal contesto di build tramite `.dockerignore`.
Per `agente` e `modelli` il server LLM sul DGX deve essere già attivo e raggiungibile
anche dal container tramite Tailscale. Questo Compose avvia il prototipo Python;
il server di inferenza rimane esterno.

## Avvio locale

Requisiti: Python 3.13 e [uv](https://docs.astral.sh/uv/).
Dalla cartella `CodiceTesi/`, lo stesso entry point si avvia con:

```bash
uv run --locked --no-editable python main.py
uv run --locked --no-editable python main.py agente "Rispondi soltanto: OK"
```

Il comando installato `tesi-agent` richiama la stessa funzione di avvio,
per esempio `uv run --no-editable tesi-agent modelli`.
La risposta del modello viene mostrata progressivamente per default.
`--no-stream` attende la risposta completa; `--stream` forza lo streaming.
Su macOS, l'attributo `hidden` applicato ricorsivamente a `.venv` può far ignorare
a Python i file `.pth` dell'installazione editable, causando
`ModuleNotFoundError: No module named 'codice_tesi'`. Per ripristinarli:

```bash
chflags -R nohidden .venv .cache
```

Le cartelle restano nascoste nel Finder grazie al punto iniziale del nome.

## Configurazione

Modello e generazione si configurano direttamente in Python tramite
**`ModelConfig(BaseModel)`**, definito in `src/codice_tesi/config.py`.
Per cambiare il comportamento della CLI, modificare i default della classe.
Da Python si può passare un'istanza personalizzata a `reply()` o `create_agent()`:

```python
from codice_tesi.config import ModelConfig

config = ModelConfig(
    model="deepseek-v4-flash",
    max_tokens=2048,
    temperature=0.2,
    top_p=0.9,
    stream=True,
)
```

I default attivi sono `deepseek-v4-flash`, 2048 token e streaming.
I parametri opzionali impostati a `None` non vengono inviati: il server conserva
i propri default. Pydantic valida sia la creazione dell'istanza sia le assegnazioni
successive, per esempio `config.temperature = 0.3`.

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
`Settings(BaseSettings)` gestisce solo connessione e credenziali. L'ordine è
argomenti Python, variabili d'ambiente, `.env`, default del codice. Per esempio:

```bash
LLM_TIMEOUT=60 uv run --no-editable python main.py agente "Rispondi soltanto: OK"
```

I parametri di `ModelConfig` vengono letti esclusivamente dal codice Python.

La risposta di `/v1/models` descrive capacità e limiti, non i valori in uso.
`context_length` e `top_provider.max_completion_tokens` (100000 dichiarati dal server)
non si modificano dal client; impostare `max_tokens` non aumenta questi limiti.
`supported_parameters` non specifica i default né i valori ammessi per `reasoning_effort`.
`top_k` e `min_p` vengono aggiunti al corpo JSON tramite `extra_body` del client.

## Chiamate API

`src/codice_tesi/api.py` raccoglie la configurazione del client HTTP e l'adattatore
che PydanticAI usa per comunicare con il DGX:

| Metodo | Endpoint | Utilizzo |
| --- | --- | --- |
| GET | `/v1/models` | `list_models(settings)` restituisce gli ID disponibili. |
| POST | `/v1/chat/completions` | L'agente invia messaggi, parametri e schemi dei tool tramite `OpenAIChatModel`. |

Il client SDK esegue le richieste HTTP e legge le risposte JSON o gli eventi SSE
di streaming. PydanticAI gestisce il ciclo dell'agente e le eventuali chiamate ai tool.
`argparse` in `src/codice_tesi/main.py` legge esclusivamente gli argomenti del terminale;
gli `add_argument` non definiscono richieste HTTP.

## Uso da Python

La CLI è solo un'interfaccia. L'agente è utilizzabile direttamente e il client HTTP
viene chiuso automaticamente all'uscita dal contesto:

```python
from codice_tesi.agent import reply
from codice_tesi.config import ModelConfig

config = ModelConfig(temperature=0.2, max_tokens=1024)


async def chiedi(prompt: str) -> None:
    async for text in reply(prompt, config):
        print(text, end="", flush=True)
    print()
```

`tools` indica le funzioni disponibili all'agente, da registrare in Python con
`@agent.tool` o `@agent.tool_plain` dentro `async with create_agent() as agent`,
importando `create_agent` da `codice_tesi.agent`. PydanticAI ne genera lo schema,
lo invia al server ed esegue le eventuali chiamate. Non sono attivi tool predefiniti:
il flag `required` da solo non ne crea e senza tool registrati produce un errore.
Dopo la prima chiamata obbligatoria, si passa ad `auto` per consentire la risposta finale.
`reply()` rispetta `config.stream` e chiude il client quando termina.
Chiamando `reply(prompt)` o `create_agent()` si usano i default di `ModelConfig`.
Per controllare direttamente l'agente usare `create_agent(config)` e
`agent.run_stream()` oppure `agent.run()`.

Ogni comando esegue una richiesta indipendente: non conserva una cronologia e non
abilita telemetria dell'agente. Per le prove iniziali usare testi sintetici.

## Sviluppo

Il codice è in `src/codice_tesi/`: `config.py` gestisce la configurazione,
`api.py` raccoglie il client e l'adattatore HTTP, `agent.py` costruisce l'agente
e produce le risposte. `main.py` coordina i comandi; `cli.py` stampa le risposte
del modello.
Le dipendenze sono in `pyproject.toml` e bloccate in `uv.lock`, da versionare insieme.

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

I test usano un endpoint simulato e non richiedono Tailscale.
L'integrazione segue la [documentazione PydanticAI per API compatibili](https://pydantic.dev/docs/ai/models/compatible-apis/).

## Dataset clinico sintetico

[Il mock v1](data/mock_clinico/v1/README.md) contiene due pazienti con ricoveri
chirurgici correnti e diciotto documenti, quindici correnti e tre precedenti.
L'[indice dei casi](data/mock_clinico/v1/pazienti/README.md) conduce alle cartelle
dei pazienti: per ogni episodio, vista strutturata e fonti sono raccolte insieme.
Il catalogo distingue la consultazione nella cartella da quella nel CDR.

Tutti i contenuti sono inventati per lo sviluppo. Il dataset rimane disponibile
come insieme di file; non è collegato al codice applicativo o all'agente.
