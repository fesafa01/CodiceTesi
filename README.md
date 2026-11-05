# CodiceTesi

Prototipo locale per preparare una bozza di lettera di dimissione nei 17 campi
Campus, con revisione del medico. Il flusso di valutazione è MIMIC-IV Hosp,
ED, ICU e referti radiologici → PostgreSQL → pacchetto della degenza → modello sullo Spark →
bozza CAMPUS. Le discharge summary MIMIC-IV-Note sono conservate separatamente
per la revisione a posteriori. Il percorso ordinario usa una richiesta. La
modalità opzionale consente letture documentali nel perimetro della degenza.

## Avvio MIMIC-IV

Con Docker e i dataset già presenti nella cartella superiore, dalla radice
del repository:

```bash
install -d -m 700 var/mimic output/dimissioni-mimic
docker compose build backend-admin
docker compose up -d --wait db
docker compose run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/../hosp:/mimic/hosp:ro" \
  -v "$PWD/../mimic-iv-note-deidentified-free-text-clinical-notes-2.2/note:/mimic/note:ro" \
  -v "$PWD/../mimic-iv-ed-2.2/ed:/mimic/ed:ro" \
  -v "$PWD/../icu:/mimic/icu:ro" \
  -v "$PWD/var/mimic:/mimic-private" \
  backend-admin import-mimic /mimic/hosp /mimic/note \
    --ed /mimic/ed --icu /mimic/icu --limit 20 --seed 42 \
    --access-file /mimic-private/access.env
docker compose up -d --build --wait backend agent-api web
```

Una volta importato il campione, basta l'ultimo comando per avviare lo stack.
La selezione legge i CSV compressi in streaming. `--replace` sostituisce
soltanto il campione MIMIC. Quando si riscrive il file di accesso, riavviare
i servizi con `docker compose up -d backend agent-api web`.

Aprire [l'interfaccia locale](http://127.0.0.1:3000) e accedere con utente
`mimic` e password `APP_MIMIC_TOKEN` dal file privato `var/mimic/access.env`.
Per la CLI, con Tailscale e serving disponibili:

```bash
umask 077
set -a
source var/mimic/access.env
set +a
uv run --locked --no-editable python main.py dimissione \
  --contesto "${APP_MIMIC_CONTEXTS%%,*}" --no-thinking --no-telemetry \
  --output-dir output/dimissioni-mimic
```

Il pairing dei 17 campi, i limiti delle fonti e il comando per confrontare una
bozza con la relativa lettera sono descritti in [MIMIC e CAMPUS](docs/mimic-campus.md).
Il [catalogo dei dataset](docs/dataset-mimic.md) include il comando per scaricare ICU.

`--thinking` e `--no-thinking` scelgono la modalità; senza flag vale `ModelConfig`
in `src/codice_tesi/config.py`. `max_tokens` comprende reasoning e testo; prima
della lettera, il reasoning attivo rinnova il limite di inattività. Connessione:
`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_TIMEOUT`. `--model` cambia l'ID richiesto:
parametri e flag devono essere verificati per ciascun modello/runtime.

`--dry-run` prepara la richiesta senza inferenza. Gli esiti sono in
`output/dimissioni/<contesto>/<esecuzione>/`; `output/dimissioni/registro/` contiene
un record per tentativo, inclusi errori e interruzioni. La validazione riguarda
schema, riferimenti delle citazioni, forma e campi istologici; il sostegno clinico
delle affermazioni resta da valutare.

La generazione restituisce le 17 sezioni in JSON con le fonti citate. Il codice
produce `bozza.txt`, `bozza_strutturata.json` ed `evidenze.md`, dove ogni sezione è
affiancata alle fonti selezionate dal modello, con origine, versione ed estratti
originali. Le evidenze individuano gli eventi della cronologia strutturata e i
paragrafi delle note lunghe; le note brevi restano intere. Il
catalogo è salvato prima dell'inferenza in `catalogo_evidenze.json`; lo stream
grezzo rimane in `risposta.parziale.json`, anche a completamento riuscito.
Riferimenti sconosciuti o incoerenti fanno fallire il controllo, conservando la
risposta grezza. Le citazioni sono per sezione e non certificano tutte le frasi.

Il template è `campus-discharge` 1.1.0: con istologico **No**, i dettagli restano
vuoti; se la risposta manca nelle fonti, viene segnalata la lacuna senza dedurre No.
I dettagli delle regole sono nei sorgenti del template e del prompt.

## Interfaccia web locale

Il client `app` è nel profilo Compose `cli`: avviare lo stack ordinario non
lancia automaticamente una seconda generazione. Per una prova CLI usare
`docker compose run --rm app ...` oppure attivare esplicitamente il profilo.
Il limite di una generazione web alla volta non coordina richieste CLI separate.

```bash
docker compose up -d --build web
```

Aprire [Dimissioni](http://localhost:3000): selezione dei casi dal DB, caricamento
preventivo del pacchetto e del template, pulsante **Genera**, bozza e fonti
cliccabili per sezione (testo originale e JSON). Il frontend usa Next.js, React,
TypeScript e Tailwind nelle versioni di `tcs-assistant`.

Il servizio `web` usa `agent-api`, adattatore HTTP del generatore esistente, e il
backend clinico. Il browser non riceve token o credenziali del modello. Le
preparazioni conservano uno snapshot delle fonti; una generazione alla volta,
con stato e risultati recuperabili ricaricando la pagina. La bozza richiede la
revisione del medico. Dettagli e avvio di sviluppo in [web/README.md](web/README.md).
Le sezioni sono modificabili e salvabili; il testo AI appare progressivamente
durante lo streaming. Le modifiche manuali vengono conservate e incluse nel
download, con il testo AI originale disponibile per confronto.

## Evidenze e approfondimenti agentici

La generazione non ha una durata massima complessiva predefinita. Solo se serve,
si può imporla dalla CLI con `--total-timeout SECONDI` o dall'API con
`timeout_seconds`. La pagina mostra fase e tempo trascorso e consente
**Interrompi**, conservando la risposta parziale. Restano i controlli di
inattività del modello e della connessione, distinti dalla durata totale.

La UI offre due caselle indipendenti: **Reasoning** (inizialmente spuntata) e
**Verifiche documentali** (inizialmente non spuntata). Le quattro combinazioni
sono disponibili anche dalla CLI con `--thinking`/`--no-thinking` e
`--tools`/`--no-tools`.

`--tools` o la casella **Verifiche documentali** abilita riepiloghi delle osservazioni, sequenze delle
terapie documentate e cronologie, oltre all’ispezione dei passaggi originali.
Tutte le fonti restano nel contesto iniziale; le elaborazioni usano soltanto lo
snapshot della degenza, con limiti di richieste, risultati e token. Questa modalità richiede
supporto alle chiamate di funzione sul serving. Il percorso ordinario usa una
sola richiesta e non richiede tool.
Phoenix riceve uno span `TOOL` per ogni elaborazione o lettura eseguita, con nome, durata,
esito e contatori dei risultati, senza argomenti o contenuti delle fonti.
Le singole inferenze hanno span `LLM` distinti; il padre `AGENT` separa i totali
del ciclo dalla dimensione massima del singolo prompt.

`fatti_documentali.json` conserva i dati riconosciuti nei record strutturati e
rilievi come quantità senza unità. Non interpreta automaticamente le note libere.
Il risultato strutturato è versione 1.1, con lettura compatibile dei precedenti
risultati 1.0. Uso, configurazione, verifiche e limiti nelle
[integrazioni agentiche](docs/integrazioni-agentiche.md).

## Accesso locale

Le API cliniche richiedono il Bearer token privato del file `var/mimic/access.env`.
Le richieste con credenziali assenti o invalide ricevono **401**. Una richiesta per
un ricovero fuori assegnazione riceve **403**, anche nella selezione casuale.
Gli healthcheck sono pubblici sul servizio locale.

## Osservabilità e verifiche

Lo stack, le configurazioni e la relativa guida sono raccolti in `observability/`.
Il `compose.yaml` principale include `observability/compose.yaml`: i comandi
restano eseguibili dalla radice con Docker Compose 2.20.0 o successivo. Registro,
metriche dello stream ed export sono nel package `src/codice_tesi/observability/`;
i relativi test sono in `tests/observability/`.

```bash
docker compose up -d --wait phoenix-db phoenix otel-collector prometheus
uv run --locked --no-editable python main.py registro --format csv \
  --destination output/metriche/esecuzioni.csv
```

Le interfacce locali sono [Phoenix](http://127.0.0.1:6006) e
[Prometheus](http://127.0.0.1:9090). Sul Mac configurare `observability/.env`
da `observability/.env.example` senza sovrascrivere impostazioni esistenti.
`--no-telemetry` disattiva l'export mantenendo il registro. I contenuti delle fonti e della bozza restano
negli artefatti locali e non sono esportati nelle tracce previste.

- [Osservabilità](observability/README.md): componenti, misure, uso e limiti.
- [Sovranità](docs/sovranita.md): protocollo, accessi, conservazione e prove aperte.
- [Verifiche svolte](docs/verifiche-sovranita.md): risultati sintetici e perimetro.
- [Tracciabilità per sezione](docs/tracciabilita.md): contratto implementato, controlli
  e limiti; [esempio JSON didattico](docs/tracciabilita.esempio.json).

Log grezzi, richieste, copie e precedenti documenti generati sono conservati sotto
`output/`, esclusi da Git e dalle immagini. I verificatori accettano destinazioni
nuove; la cancellazione dimostrativa opera soltanto su copie censite, senza
pulizie programmate degli archivi correnti.

## Test

La suite usa fixture autonome per verificare persistenza, isolamento degli
accessi, selezione di fonti e contratti delle bozze.

```bash
uv sync --locked --extra backend --no-editable
uv run --locked --extra backend --no-editable ruff check .
uv run --locked --extra backend --no-editable ruff format --check .
# CLINICAL_TEST_POSTGRES_URL deve indicare PostgreSQL di test.
uv run --locked --extra backend --no-editable pytest -q
```

I test SQL creano e rimuovono schemi isolati; senza la variabile di connessione
sono saltati. Le istruzioni per il DB temporaneo della conservazione sono nel
protocollo.
