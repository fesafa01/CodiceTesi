# Unica richiesta Campus in streaming

Il programma recupera una sola volta dal backend tutte le note dello stesso
`note_id`. Costruisce due messaggi per una sola chiamata allo Spark:

- Istruzioni: regole di fedeltà e tutti i 17 campi Campus, con titoli numerati e sottocampi.
- Prompt: contesto verificato e tutte le note, inserite una sola volta.

Il modello redige tutta la lettera in una singola risposta in streaming, seguendo
l'ordine dei campi indicato nelle istruzioni. I campi già scritti fanno parte della
stessa generazione: non vengono reinviati con altre chiamate. L'applicazione non
esegue più un ciclo di richieste per sezione e non scrive intestazioni al posto
del modello. Il tipo di nota non esclude alcun campo.

## Richiesta e output

`discharge/prompts.py` legge i campi da `templates/campus_discharge.v1.json`.
Le note complete sono nel prompt senza duplicare le ripetizioni del pacchetto
HTTP. L'agente PydanticAI non ha tool, accesso al database o history di altre
esecuzioni. Il limite applicativo è una richiesta (`request_limit=1`), senza retry
nell'agente o nel client HTTP.

`ModelConfig.max_tokens` è il limite dell'intera lettera, default 8192.
`--context-window` è il budget della richiesta completa, default 32768 stimati,
compresi istruzioni, note, output riservato e margine. Un superamento ferma il
programma prima dell'inferenza: non tronca note e non suddivide in più chiamate.
La stima in byte UTF-8 più framing è prudenziale, non il tokenizer del server.

`--dry-run` salva lo stesso prompt della generazione, senza client LLM, testi
segnaposto o una bozza fittizia. A parità di contesto e configurazione, istruzioni
e note sono identiche fra dry-run e generazione.

## Streaming e salvataggio

`application.py` usa un solo iteratore dello stream. Per ogni frammento esegue
immediatamente scrittura e flush nello stesso file e stampa nel terminale.
I file di ogni cartella di esecuzione sono:

- `bozza.parziale.txt`: il testo durante la generazione o dopo un'interruzione.
- `bozza.txt`: lo stesso file rinominato dopo uno stream terminato normalmente e non vuoto.
- `richiesta.txt`: istruzioni e prompt effettivi, un'unica richiesta.
- `esito.json`: contesto, fonti, campi previsti, parametri, conteggio delle richieste ed esito.

Ctrl+C, errore di trasporto o raggiungimento del limite di output conservano
il parziale. Non si apre un'altra richiesta per completarlo. La ripresa automatica
non è implementata; un nuovo avvio crea una nuova cartella.

Il timeout di lettura dello stream è disabilitato, mentre restano i timeout di
connessione, scrittura e pool. Questo non accelera il modello né impedisce limiti
imposti dal server; prima del primo testo può esserci attesa.

L'esito tecnico non certifica correttezza clinica, completezza o rispetto dei
titoli. La redazione campo per campo è affidata alle istruzioni al modello, senza
validazione strutturale del testo libero. La bozza richiede revisione del medico.

## Comandi

Dalla cartella `CodiceTesi/`, con backend popolato e Spark attivo:

```bash
uv run --locked --no-editable python main.py dimissione --contesto ecream-note-2136
uv run --locked --no-editable python main.py dimissione --contesto ecream-note-2136 --dry-run
```

Il [README](../README.md#comando-locale-completo-e-opzioni) spiega tutti i flag.
`--contesto` e `--seed` sono alternativi. Per aggiornare l'immagine dell'app e
avviarla in Docker, salvando i risultati sul computer:

```bash
mkdir -p var/runs
docker compose run --build --rm \
  -v "$PWD/var/runs:/app/var/runs" \
  app dimissione --contesto ecream-note-2136
```

Il backend usa ancora `campus-context-v2`, con tutte le fonti del contesto
riportate per ogni sezione del pacchetto HTTP. L'applicazione ne invia un solo
insieme allo Spark. Non servono migrazioni del DB o una nuova importazione.
Resta supportato il raggruppamento di testi distinti per `note_id`, con deduplica
dei testi identici ripetuti per annotazioni. Le API FHIR sono esterne al flusso.
