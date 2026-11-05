# Registro sperimentale e osservabilità locale

Il registro JSON per esecuzione è il riferimento per l'analisi della tesi. Phoenix
visualizza le tracce e le singole chiamate LLM; Prometheus conserva gli istogrammi
e i contatori aggregati. Un valore assente resta `null`: non viene sostituito con
zero, né ricavato dal numero di caratteri o chunk.

## Organizzazione dei file

```text
observability/
  compose.yaml       # Phoenix, DB tracce, Collector, Prometheus e volumi
  collector.yaml     # Pipeline OTLP e coda persistente
  prometheus.yaml    # Scrape delle metriche
  spark_usage.py     # Telemetria DGX Spark e campioni nella finestra di una prova
  .env.example       # Configurazione pubblica per il client sul Mac
  .env               # Configurazione locale esclusa da Git e immagini
  README.md          # Uso, definizioni delle misure e limiti
src/codice_tesi/observability/
  execution.py       # Registro atomico, manifest ed esportazione CSV/JSONL
  spark.py           # Acquisizione locale delle misure Spark per run_id
  stream_metrics.py  # Osservazioni dello stream, senza conservarne i testi
  telemetry.py       # Configurazione ed export OpenTelemetry
tests/observability/ # Test del registro, delle misure e dell'isolamento dei dati
```

Tutti i comandi Docker di questa pagina si eseguono dalla radice `CodiceTesi`.
Il Compose principale include lo stack: nomi di servizi, progetto, reti e volumi
restano quelli esistenti. Non avviare il Compose incluso come progetto separato.
Sono richiesti Docker Compose con supporto a `include` (2.20.0 o successivo).
L'applicazione mantiene soltanto i collegamenti al Collector e ai moduli Python.
Il registro resta disponibile anche quando l'export è disabilitato.

La [raccolta sul DGX Spark](Monitoraggio_DGX_Spark.md) si avvia automaticamente
a fine prova e usa gli orari UTC del registro per associare i campioni hardware
al `run_id`. Lo stato della raccolta compare in `spark_monitor`.

## Architettura

```mermaid
flowchart LR
    APP[Applicazione CLI] --> REG[Registro JSON atomico e artefatti locali]
    APP -->|OTLP HTTP| COL[OpenTelemetry Collector]
    COL -->|Tracce con coda persistente| PX[Phoenix]
    PX --> PDB[(PostgreSQL dedicato)]
    COL -->|Metriche aggregate| EXP[Endpoint Prometheus interno]
    PROM[Prometheus] -->|scrape ogni 5 s| EXP
```

Servizi definiti in `observability/compose.yaml`, incluso dal Compose principale:
`phoenix`, `phoenix-db`, `otel-collector`, `prometheus`. `otel-storage-init` è un servizio iniziale che prepara i permessi del
volume e termina; il Collector viene eseguito con l'utente non privilegiato della
sua immagine. Il database Phoenix è separato dal database dei ricoveri.

| Interfaccia | Indirizzo host | Uso |
|---|---|---|
| Phoenix | <http://127.0.0.1:6006> | Progetto `tesi-dimissioni`, trace, span LLM e metadati |
| Prometheus | <http://127.0.0.1:9090> | Query e grafici delle metriche aggregate |
| Ricezione OTLP | `http://127.0.0.1:4318` | `/v1/traces` e `/v1/metrics` per processi sul Mac |

Solo queste porte sono pubblicate e soltanto sul loopback. Database Phoenix,
endpoint di scrape e gRPC non sono pubblicati. La rete Docker dedicata isola i
servizi di osservabilità dagli altri servizi, salvo il Collector che riceve anche
dalla rete applicativa. Questa configurazione locale usa Phoenix senza login;
non è una configurazione da esporre su una rete condivisa.

Le immagini hanno versioni fissate; PostgreSQL Phoenix usa anche un digest.
`uv.lock` fissa le dipendenze Python. Phoenix ha disabilitati telemetria web e
caricamento autonomo di risorse esterne. Questo non equivale a una verifica di
funzionamento air-gapped dell'intero sistema.

## Avvio e uso

Dalla cartella `CodiceTesi`:

```bash
docker compose up -d --wait phoenix-db phoenix otel-collector prometheus
docker compose build app

# Backend e campione già inizializzati:
docker compose run --rm app dimissione --seed 42 --dry-run --no-telemetry \
  --experiment-file /app/experiments/exploratory.example.json
```

La cartella `experiments/` è montata in sola lettura nel container. Creare un JSON
per la condizione sperimentale e aggiornare `repetition` a ogni ripetizione.
`--model` cambia l'ID richiesto al server; i parametri e i flag di reasoning del
modello vanno verificati per ogni famiglia, senza supporre che siano portabili.

Per l'esecuzione Python sul Mac:

```bash
# Eseguito già durante l'integrazione; non sovrascrivere una configurazione personale.
cp -n observability/.env.example observability/.env
uv sync --locked --extra backend --no-editable
set -a; source var/mimic/access.env; set +a
uv run --locked --no-editable python main.py dimissione \
  --seed 42 --dry-run --no-telemetry \
  --experiment-file experiments/exploratory.example.json
```

Il file `observability/.env` è separato da `.env`, destinato alla connessione LLM.
Variabili: `TELEMETRY_ENABLED`, `TELEMETRY_ENDPOINT`, `TELEMETRY_PROJECT`,
`TELEMETRY_EXPORT_TIMEOUT_SECONDS`. Nel container l'endpoint è
`http://otel-collector:4318`; sul Mac è `http://127.0.0.1:4318`.
`--no-telemetry` disabilita gli export; il registro locale resta attivo.

Le generazioni continuano a richiedere backend, dataset e Spark raggiungibili.
L'elenco dei modelli è verificabile senza inferenza con `python main.py modelli`.
Se i servizi sono già avviati, `docker compose run --rm --no-deps app ...` permette
anche di eseguire il client quando lo stack di osservabilità non è disponibile.
Gli errori di esportazione non devono trasformare una generazione riuscita in un
fallimento. Un Collector non raggiungibile può comunque perdere dati prima della
ricezione: il registro rimane il riferimento e non viene ricreato dalla dashboard.

## Registro e riproducibilità

Ogni invocazione del workflow, compresi `--dry-run`, fallimenti di lettura o
preparazione, errori del modello e cancellazioni, crea:

```
output/dimissioni/registro/<run_id>.json
```

Quando il pacchetto è disponibile, il record rimanda anche alla cartella originale
`<contesto>/<esecuzione>/`, con `richiesta.txt`, bozza o parziale ed `esito.json`.
Quest'ultimo mantiene i campi esistenti e aggiunge `run_id` e `trace_id`.

Il record contiene:

- schema del registro, UUID, timestamp UTC, stato ed eventuale tipo di errore;
- esperimento, condizione e ripetizione, distinti dal seed di selezione del caso;
- modello richiesto e modello riportato dalla risposta;
- configurazione richiesta, hash del codice installato, versioni dei pacchetti;
- hash del pacchetto completo e della richiesta, versioni di dataset, prompt e
  template, quantità di fonti, configurazione del contesto e hash della bozza;
- tempi delle fasi e misure dello stream, conteggi di token disponibili e qualità dei contatori;
- identificativi della trace, percorso degli artefatti e risultato del flush SDK.

Checkpoint, tokenizer, quantizzazione, runtime, hardware di inferenza, cache,
concorrenza e split del dataset sono **dichiarati dall'operatore** nel JSON
esperimento. Un campo non verificato va lasciato `null`/`unknown`. Il sistema non
attribuisce allo Spark le caratteristiche del Mac o del container client.
`source_sha256` identifica i sorgenti e i template installati, non un commit Git.
Nell’installazione il manifest segue i file dichiarati nel pacchetto distribuito,
escludendo eventuali copie residue non appartenenti alla distribuzione.
Per `deepseek-v4-flash` e `deepseek-v4-pro`, il controllo del contesto usa il tokenizer
ufficiale DeepSeek V4 incluso nel pacchetto e il template testuale system/user/assistant
di DS4, con la modalità thinking richiesta. Il conteggio è locale e funziona offline.
Metodo e revisione del tokenizer sono registrati in `input_estimate_method` e
`input_tokenizer_revision`, anche negli esiti. È una stima del prompt del runtime:
il conteggio effettivo resta quello restituito dal server. Il controllo riserva
l'intero limite di output e il margine di sicurezza di 1.024 token entro il budget
di 100.000 token. Per ID di modelli diversi resta la stima prudenziale in byte UTF-8
più 512, segnalata esplicitamente nel metodo; non viene applicato il tokenizer DeepSeek.

Scrittura atomica per ogni record e nessun file CSV condiviso in append: processi
concorrenti scrivono UUID distinti. Il checkpoint ogni 15 secondi aggiorna lo stato
in corso. Dopo SIGKILL o perdita di alimentazione un record può restare
`preparing`/`running`; non deve essere interpretato come esecuzione completata.
Le durate usano un orologio monotono, i timestamp UTC servono alla correlazione.

```bash
uv run --locked --no-editable python main.py registro --format csv \
  --destination output/metriche/esecuzioni.csv
uv run --locked --no-editable python main.py registro --format jsonl \
  --destination output/metriche/esecuzioni.jsonl

# Alternativa dal container, con output persistente sul Mac:
docker compose run --rm --no-deps app registro --format csv \
  --destination output/metriche/esecuzioni.csv
```

L'export legge soltanto il nuovo registro; non inventa metriche per le bozze
storiche. JSONL conserva oggetti e `null`; CSV appiattisce i nomi con punti e rende
`null` come cella vuota. L'export comprende anche errori, dry-run e tentativi ancora
in corso: selezionare stati e modalità prima di calcolare statistiche scientifiche.

## Definizioni delle misure

| Campo di `llm.requests[]` del registro | Significato |
|---|---|
| `request_duration_seconds` | Durata della richiesta di modello fino alla chiusura dello stream, osservata dal client |
| `time_to_first_chunk_seconds` | Primo chunk strutturato ricevuto dall'adattatore, anche se contiene soltanto il ruolo |
| `time_to_first_content_seconds` | Primo contenuto di testo o reasoning, compreso whitespace |
| `time_to_first_reasoning_seconds` | Primo reasoning non vuoto nei campi supportati |
| `time_to_first_text_seconds` | Primo testo non vuoto della risposta, anche un preambolo prima di un tool |
| `time_to_last_content_seconds` | Ultimo contenuto osservato, distinto dal chunk finale di usage |
| `content_chunk_gap_mean_seconds` / `max` | Distanze tra chunk con contenuto; non latenza tra token |
| `input_tokens` / `output_tokens` | Conteggi dello snapshot usage del server |
| `reasoning_tokens` | Dettaglio esplicitamente riportato dal server; `null` se assente |
| `visible_output_tokens` | Output meno reasoning, se entrambi disponibili e coerenti con il contratto Chat Completions |
| `total_tokens` / `calculated_total_tokens` | Totale dichiarato e somma input/output, conservati separatamente |
| `cached_input_tokens` | Conteggio riportato nei dettagli di input, se disponibile |
| `uncached_input_tokens` | Input meno cache dichiarata, solo se entrambi disponibili e coerenti; non misura il lavoro fisico della GPU |
| `request` | Indice, parametri della richiesta, numero di tool definiti, struttura della storia e controllo finale in coda |
| `finish_reason` / `tool_calls_requested` | Termine della singola risposta e numero di tool call emesse |
| `output_tokens_per_request_second` | Output token / durata della richiesta client, per stream completati e usage coerente |
| `server_reported_timings` | Solo campi numerici noti dell'oggetto `timings`, quando presente |
| `server_decode_tokens_per_second` | `predicted_n / predicted_ms * 1000`, solo se questi campi sono riportati dal backend |
| `server_ttft_seconds` | Attualmente `null`: il client non osserva il TTFT interno del motore |

`usage_complete` richiede sia uno snapshot usage sia la conclusione dello stream;
non indica che la bozza sia corretta o abbia superato la validazione. Una risposta
troncata/interrotta conserva i contatori disponibili senza presentarli come totali
completi. Gli snapshot usage cumulativi sostituiscono i precedenti: non si sommano.
Il reasoning è un sottoinsieme dell'output nel contratto adottato e non si aggiunge
nuovamente al totale. Un backend che segue un contratto diverso richiede un
adattatore dedicato; le incongruenze rilevate sono in `usage_issues`.

Queste misure comprendono effetti di rete e consumo dello stream da parte del
client, inclusi flush del file e stampa. La velocità client non è la velocità pura
di decode del motore. Coda, prefill e TTFT server richiedono osservazioni del
serving attualmente non attestate. La misurazione di potenza ed energia
richiede un metodo dedicato ancora da definire.
Token/s e budget di token vanno interpretati insieme a tokenizer, configurazione,
lunghezza della risposta e qualità, senza equiparare tokenizer diversi.

`timings.total_seconds` misura il workflow fino all'esito locale, escluso il flush
finale della telemetria. Le fasi comprendono lettura/validazione del pacchetto,
preparazione della richiesta, salvataggio iniziale, generazione e validazione/salvataggio
della bozza. La fase di recupero misura il client del backend; non scompone SQL e
rete nel server documentale. Il tempo di revisione medica non è misurato da questo stack.

## Phoenix: che cosa guardare

Aprire il progetto `tesi-dimissioni`. `tesi.run_id` collega la trace al record
locale. Le nuove esecuzioni distinguono tre livelli:

```text
discharge.execution                         CHAIN
└─ generate_draft                           CHAIN
   └─ discharge.agent_run                   AGENT
      ├─ chat.request.1                     LLM
      ├─ tool riepiloga_osservazioni         TOOL
      └─ chat.request.2                     LLM
```

Solo gli span `LLM` hanno `llm.token_count.*` e `gen_ai.usage.*`: ogni richiesta
contribuisce una sola volta ai totali standard di Phoenix. Il padre `AGENT` ha
riepiloghi applicativi separati. Anche senza tool esistono un padre AGENT e un
figlio LLM. Le trace storiche conservano la precedente struttura aggregata e
non vengono riscritte.

| Gruppo | Contenuto |
|---|---|
| AGENT `tesi.prompt.*` | Stima iniziale locale e sua separazione in istruzioni, fonti e framing, numero di fonti/passaggi; `full_context=true` |
| AGENT `tesi.agent.usage.*` | Totali di input/output/cache, input della prima richiesta, massimo input di una singola richiesta e somma degli input successivi |
| AGENT `tesi.agent.timing.*` | Durata del ciclo, somma delle durate dei provider, intervalli tra richieste, primo testo qualsiasi e primo testo della risposta finale |
| AGENT `tesi.agent.tools.*` | Tentativi, rifiuti e operazioni eseguite/completate/fallite |
| LLM `tesi.request.context.*` | Stima locale, modalità della storia (`final_append` nella fase finale), strategia del prefisso, conteggi di messaggi, risultati dei tool e feedback di correzione |
| LLM `tesi.request.tools.*` | Definizioni presenti e `execution_enabled`: nella fase finale gli schemi restano stabili ma le operazioni sono bloccate |
| LLM `tesi.request.budget.*` | Massimo output di quella richiesta e motivo del passaggio alla fase finale |
| LLM `tesi.request.reasoning.*` / `sampling.*` | Parametri passati per quella richiesta; non i soli massimali di configurazione |
| LLM `tesi.request.cache.*` | Dato del provider: `unreported`, `reported_zero`, `reported_reuse` o `inconsistent` |
| LLM `tesi.request.timing.*` / `stream.*` | Tempi e contatori del singolo stream; `phase` distingue selezione tool, risposta finale e stream incompleto |
| TOOL `tesi.tool.request_index` | Indice della richiesta che ha originato la chiamata |

`tesi.prompt.instruction_tokens`, `data_prompt_tokens` e
`source_text_tokens_sum` sono conteggi con il tokenizer locale supportato.
`framing_and_notices_tokens_approx` e `chat_template_tokens_approx` sono
differenze approssimate: i confini di tokenizzazione incidono sul conteggio.
Non includono gli schemi dei tool e non vengono esposti per modelli il cui
tokenizer non è disponibile. Il consumo effettivo resta quello del provider.

Esempio: 42.262 token nella prima richiesta e 44.097 nella seconda diventano
86.359 token di input totali; il massimo contesto osservato in una singola
richiesta resta 44.097. `subsequent_request_input_tokens_total` non misura token
nuovi, né quantifica esattamente testo duplicato: conta gli input delle richieste
successive. La cache è una dichiarazione del serving, non una misura indipendente
delle operazioni GPU. Un dato assente resta sconosciuto e non diventa zero.

Gli span sono espliciti OTel, senza auto-instrumentazione duplicata del framework.
Nessun costo monetario viene calcolato per il modello locale. `flush_completed`
certifica soltanto che il flush SDK ha terminato: non è una ricevuta di
persistenza in Phoenix.

Prompt, output, fonti, identificativi di paziente/episodio e reasoning non sono
esportati. Per le eccezioni si esporta soltanto il tipo, senza messaggio o stack
trace, che potrebbero includere dati clinici. La richiesta e la bozza rimangono
negli artefatti locali già previsti: la gestione del loro accesso resta necessaria
quando il dataset cambierà. La dashboard non offre il replay del prompt perché il
contenuto non è stato caricato.

La [politica dimostrativa di conservazione](../docs/sovranita.md#conservazione-e-cancellazione-dimostrative) è
verificata su nuove copie locali e fixture su file di log/tracce/metriche/cache.
Non applica la scadenza di 24 ore ai servizi in esercizio: Prometheus mantiene
la retention di 90 giorni configurata in Compose; persistenza Phoenix, coda del
Collector, log Docker e serving richiedono regole e verifiche separate.

## Prometheus e comandi brevi

Il client esporta metriche con temporalità **delta**; il processore
`deltatocumulative` del Collector accumula i valori tra processi CLI successivi.
Senza questa conversione un istogramma cumulativo nuovo per ogni comando potrebbe
sostituire quello precedente. Le etichette contengono progetto, modello, modalità thinking,
stato e modalità dry-run; non contengono run ID, trace ID o identificativi clinici.
Gli istogrammi hanno bucket espliciti adatti a secondi e quantità di token.

Metriche principali:

- `tesi.executions`: tentativi del workflow, compresi dry-run e fallimenti;
- `tesi.execution.duration`: durata totale del workflow;
- `tesi.stage.duration`: tempi per fase;
- `gen_ai.client.operation.duration`: durata della richiesta LLM;
- `gen_ai.client.operation.time_to_first_chunk`: attesa al primo chunk;
- `tesi.llm.time_to_first_text`: attesa al primo testo della lettera;
- `gen_ai.client.token.usage`: input/output riportati, senza doppio conteggio del reasoning;
- `tesi.llm.reasoning_tokens`: dettaglio separato, da non sommare nuovamente all'output;
- `tesi.llm.output_tokens_per_request_second`: distribuzione della velocità client per richiesta.

Le metriche GenAI sono registrate per singola richiesta; la somma dei token non
viene registrata nuovamente come un altro campione. `tesi.agent.duration` e
`tesi.agent.requests` misurano separatamente durata del ciclo e numero di richieste.
I conteggi token entrano negli aggregati solo per stream conclusi, con usage
disponibile e coerente; i record conservano anche le misure parziali.
Phoenix riceve **tracce**, Prometheus **metriche**: gli istogrammi non vengono inviati
a Phoenix come se fosse un database di metriche.

La conversione dei nomi OTel per Prometheus aggiunge unità/suffissi e normalizza i
punti in underscore. Usare il selettore metriche dell'interfaccia per confermare i
nomi esposti. Il registro JSON resta la fonte per confronti esatti tra casi e
configurazioni; gli istogrammi sono strumenti di monitoraggio, non sostituiscono i
dati individuali dell'esperimento.

Esempi di query sui nomi verificati nello stack locale:

```promql
# Esecuzioni completate con inferenza, escludendo dry-run
sum(tesi_executions_total{tesi_status="completed",tesi_dry_run="false"})

# Token di output riportati nelle richieste complete
sum(gen_ai_client_token_usage_sum{gen_ai_token_type="output",tesi_dry_run="false"})

# Latenza media delle richieste del segmento corrente del Collector
sum(gen_ai_client_operation_duration_seconds_sum{tesi_status="completed"})
/
sum(gen_ai_client_operation_duration_seconds_count{tesi_status="completed"})
```

Per confronti controllati usare il registro e filtrare anche hash del codice,
versione del dataset e condizione. Un istogramma di monitoraggio può aggregare
esecuzioni con configurazioni diverse e non sostituisce il campione sperimentale.

## Persistenza, arresto e verifica

I volumi `phoenix-db`, `otel-queue` e `prometheus-data` conservano rispettivamente
tracce, coda di esportazione e serie temporali. Prometheus conserva 90 giorni.
La coda trace ritenta senza scadenza temporale ma ha capacità finita: non garantisce
assenza di perdite se il disco è pieno o il Collector non riceve i dati.
L'accumulazione delta delle metriche è in memoria nel Collector: un suo riavvio
azzera questa accumulazione; Prometheus conserva i campioni precedenti e deve
trattare il nuovo segmento come un reset. Le serie inattive scadono dopo 72 ore.

```bash
docker compose ps
docker compose logs --tail 40 otel-collector phoenix
docker compose run --rm --no-deps otel-collector validate \
  --config=/etc/otelcol/config.yaml

# Arresto del solo stack, senza cancellazione dei volumi:
docker compose stop prometheus otel-collector phoenix phoenix-db
```

Non usare `down -v` per un normale arresto. Per le prove automatiche:

```bash
TELEMETRY_ENABLED=false CLINICAL_TEST_POSTGRES_URL=postgresql://clinico:clinico_sintetico@127.0.0.1:5433/clinico \
  uv run --locked --extra backend --no-editable pytest -q
uv run --locked --extra backend --no-editable ruff check .
```

Riferimenti tecnici consultati il 23 ottobre 2026:
[Phoenix](https://arize.com/docs/phoenix/self-hosting/deploying-phoenix),
[OpenTelemetry Collector: resilienza](https://opentelemetry.io/docs/collector/resiliency/),
[convenzioni GenAI](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/model/gen-ai/metrics.yaml).
Le convenzioni GenAI sono ancora in evoluzione; il registro ha uno schema proprio
versionato e non dipende dall'organizzazione interna del database Phoenix.

## Cicli con strumenti documentali

`llm.requests[]` conserva le misure per ogni richiesta, anche per il percorso
senza tool. I campi al livello `llm` conservano gli aggregati del ciclo per
compatibilità; `cycle_duration_seconds` ne rende esplicita la durata.
`provider_requests_duration_seconds` somma i singoli stream e
`between_requests_seconds` comprende tool, preparazione e altro lavoro del client:
non è una misura esclusiva del tempo dei tool. I token aggregati sommano ogni
richiesta una sola volta; se manca un contatore l'aggregato rimane sconosciuto.
`measurement_boundary=agent_run_client` distingue il tempo complessivo del ciclo,
che comprende gli intervalli dei tool, dal tempo della singola richiesta.
Il primo testo può essere un preambolo precedente a un tool: non equivale alla
disponibilità della lettera finale.

Ogni tool documentale eseguito produce uno span figlio del padre AGENT,
nella stessa trace, con `openinference.span.kind=TOOL` e `tool.name`.
Phoenix mostra nome, durata e stato; gli attributi `tesi.tool.*` aggiungono indice della richiesta e indice
della chiamata, esito, numero dei risultati, corrispondenze totali, byte restituiti e indicazione
di risultati limitati. Gli errori hanno stato `ERROR` e solo il tipo
dell'eccezione, senza messaggio. Query, argomenti, ID delle evidenze, testo e
risultati completi non vengono esportati. L'audit dettagliato degli ID consultati
rimane nell'esito locale.

Le chiamate rifiutate prima della funzione hanno uno span `TOOL` separato,
`tesi.tool.status=rejected`, `tesi.tool.arguments_valid=false` e
`tesi.tool.attempt_index`. Il nome viene esportato solo se appartiene ai quattro
tool autorizzati; altrimenti è `tool_sconosciuto`. Il registro distingue questi
tentativi (`tools.attempts`) dalle operazioni effettive (`tools.calls`). Una
correzione guidata consuma i budget esistenti e non è un retry di trasporto.

Le scelte reasoning/tool sono salvate nel registro e nella metadata della trace,
oltre agli attributi `tesi.thinking` e `tesi.tools_enabled` sullo span radice.
Il testo interno del reasoning non viene esportato; tempi e token restano
disponibili quando il serving li riporta. Senza tool abilitati o senza operazioni
richieste dal modello, non compaiono span `TOOL`. Non è necessaria
l'auto-instrumentazione del framework.
