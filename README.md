# CodiceTesi

L'applicazione invia **una sola richiesta allo Spark** con tutte le note del
`note_id` selezionato e tutti i campi Campus nelle istruzioni. Il modello redige
la bozza in ordine, campo per campo, in una sola risposta in streaming.
Ogni frammento viene mostrato nel terminale e salvato nello stesso TXT.

Il backend raggruppa le note deterministicamente. Il tipo di nota non esclude
alcun campo. L'agente valuta quali informazioni siano disponibili; dove mancano
dati scrive una breve indicazione. L'ordine dei campi è richiesto nel prompt,
non gestito con chiamate separate. L'agente non ha tool né accesso al database.

## Avvio

Dalla cartella `CodiceTesi/`, con Docker e Spark attivi:

```bash
docker compose up --build -d backend
uv run --locked --no-editable python main.py dimissione \
  --contesto ecream-note-2136 \
  --output-dir var/runs \
  --backend-url http://127.0.0.1:8001 \
  --context-window 32768
```

Il database di sviluppo è già popolato. Un nuovo database nasce vuoto: per
importare il dataset pubblico eCREAM già scaricato, usare esplicitamente:

```bash
docker compose run --rm \
  -v "$PWD/../datasets_pubblici/ecream_gamma_span:/dataset:ro" \
  backend-control import-ecream /dataset/notes.jsonl
```

Il server LLM dello Spark deve essere raggiungibile tramite Tailscale.
Compose gestisce backend e PostgreSQL; il server LLM è esterno.
Le API del backend sono disponibili su <http://localhost:8001/docs>.
Per l'avvio locale servono Python 3.13 e `uv`.

## Comando locale completo e opzioni

| Parte del comando | Significato |
| --- | --- |
| `docker compose up` | Avvia il backend e le dipendenze, incluso PostgreSQL. |
| `--build` | Aggiorna le immagini dai sorgenti. |
| `-d` | Lascia i servizi attivi in background. |
| `backend` | È il servizio da avviare. |
| `uv run` | Esegue nell'ambiente Python del progetto, sincronizzando le dipendenze. |
| `--locked` | Richiede che `uv.lock` rimanga invariato e sia coerente col progetto. |
| `--no-editable` | Installa il pacchetto normalmente; la cache segue sorgenti e template. |
| `python main.py dimissione` | Genera tutta la lettera con una sola richiesta in streaming. |
| `--contesto` | ID del contesto del database da elaborare, senza scelta casuale. |
| `--output-dir` | Directory base dei risultati; viene creata una sottocartella per esecuzione. |
| `--backend-url` | URL del backend clinico, non dello Spark. |
| `--context-window` | Budget stimato della richiesta: note, istruzioni Campus, output e margine. |

Le barre `\` continuano il comando sulla riga successiva.
Aggiungere `--dry-run` prepara l'unica richiesta completa, senza inferenza.
Non contiene segnaposto di campi precedenti: è lo stesso prompt usato dalla
generazione a parità di dati e configurazione.
`--seed 42` può sostituire `--contesto` per una selezione casuale riproducibile;
le due opzioni sono alternative. Il seed non rende deterministico il testo LLM.
Senza argomenti parte `dimissione` con un seed registrato automaticamente.
`modelli` elenca i modelli disponibili sullo Spark.

`APP_BACKEND_URL` o `--backend-url` sceglie il backend: default locale
`http://127.0.0.1:8001`, in Compose `http://backend:8001`.

## Dove si trova la bozza

Il programma stampa subito la directory di esecuzione e il riepilogo:
numero di note, 17 campi Campus e **una richiesta LLM**.
Ogni avvio crea una cartella distinta sotto `var/runs/` o `--output-dir`:

- **`bozza.parziale.txt`**: unico file aperto e aggiornato a ogni frammento dello stream.
- **`bozza.txt`**: lo stesso file rinominato alla conclusione normale della risposta.
- `richiesta.txt`: istruzioni con i campi Campus e tutte le note, ciascuna inserita una sola volta.
- `esito.json`: contesto, fonti, configurazione, consumo ed esito tecnico sintetico.

Ctrl+C, errore di connessione, risposta vuota o limite di output conservano il
parziale e impediscono la pubblicazione di `bozza.txt`. Non ci sono retry automatici.
Il testo non viene ricostruito né riscritto tra campi e non ci sono file per sezione.
I risultati delle esecuzioni precedenti restano nelle rispettive cartelle.

La bozza richiede revisione del medico: una risposta terminata normalmente non
certifica correttezza clinica, completezza o rispetto delle intestazioni.
L'ordine e la copertura dei campi sono istruzioni al modello, non una validazione
strutturale del testo libero.

In Docker i risultati persistono nel volume `discharge-runs`, su `/app/var/runs`.
Per salvare direttamente i TXT sul computer:

```bash
mkdir -p var/runs
docker compose run --build --rm \
  -v "$PWD/var/runs:/app/var/runs" \
  app dimissione --contesto ecream-note-2136
```

`run` crea il container del servizio `app`, `--build` aggiorna l'immagine,
`--rm` rimuove il container al termine e `-v` collega la directory locale ai
risultati nel container. L'URL backend interno è già configurato da Compose.

## Configurazione e streaming

`ModelConfig` in `src/codice_tesi/config.py` contiene i parametri del modello:
`deepseek-v4-flash`, temperatura `0`, `8192` token di output per l'intera lettera,
`reasoning_effort="low"`. Da Python si può passare `ModelConfig(...)` a
`run_application()`; `None` omette un parametro facoltativo.

`ContextConfig` in `discharge/config.py` imposta il budget della singola richiesta:
`32768` token stimati, inclusi input, output riservato e margine. La stima usa i
byte UTF-8 più un framing prudenziale, non il tokenizer del modello.
Un budget insufficiente interrompe prima dell'inferenza senza tagliare le note.

La connessione usa `Settings` e il `.env` facoltativo, escluso da Git:

```dotenv
LLM_BASE_URL=http://spark-413e.tail9c981a.ts.net:8000/v1
LLM_API_KEY=not-required
LLM_TIMEOUT=300
```

La generazione non ha timeout di lettura. Restano i timeout di connessione,
scrittura e pool; le richieste ordinarie come `modelli` hanno un timeout completo.
Lo streaming non elimina l'attesa iniziale del server. Ctrl+C permette di
interrompere conservando il testo. Il ragionamento interno non viene stampato.

## Codice, dati e verifiche

`application.py` recupera il contesto, prepara una richiesta e salva lo stream.
`discharge/prompts.py` è l'unica sede delle istruzioni e del prompt;
`templates/campus_discharge.v1.json` è la definizione canonica dei campi.
`generator.py` contiene l'unico agente, `context.py` verifica il budget e
`artifacts.py` conserva richiesta, testo ed esito.
Il backend mantiene il contratto `campus-context-v2`; questa modifica non
richiede migrazioni del database né cambiamenti alle note importate.

Il JSONL eCREAM locale contiene 4.047 ID distinti, attualmente un testo per ID.
L'importatore supporta più testi dello stesso `note_id`, deduplica quelli identici
ripetuti per annotazioni e tiene separati ID diversi. Il raggruppamento documentale
non attribuisce identità di paziente o episodio. Le API FHIR restano esterne al flusso.

```bash
uv run --locked --extra backend --no-editable pytest
uv run --locked --extra backend --no-editable ruff check .
uv run --locked --extra backend --no-editable ruff format --check .
```

I test SSE usano un endpoint simulato. I test PostgreSQL richiedono
`CLINICAL_TEST_POSTGRES_URL` e usano schemi temporanei isolati.
Le chiavi di cache uv includono sorgenti e template per aggiornare il pacchetto
anche con `--no-editable`. Le immagini usano `uv.lock` e un utente senza privilegi.

La [guida](docs/compilazione.md) descrive il flusso e i comandi.
La [guida del backend](docs/backend.md) documenta importazione e API.
