# CodiceTesi

Prototipo locale per preparare una bozza di lettera di dimissione nei 17 campi
Campus, con revisione del medico. Il flusso è fonti FHIR → PostgreSQL → pacchetto
validato → una richiesta al modello sullo Spark → bozza o parziale con esito.

Il materiale Synthea attuale è sintetico e provvisorio: include scenari di uscita
aggiunti da regole riproducibili, senza LLM e senza validazione clinica. Non è il
dataset definitivo della valutazione. Originali, dati di prova e output rimangono
esterni al codice versionato.

## Avvio

Dalla cartella del repository, con Docker, Tailscale e serving LLM disponibili:

```bash
docker compose up --build -d backend
# Solo per inizializzare il campione sintetico, quando necessario:
docker compose run --rm backend-admin import-synthea /dataset --replace
uv run --locked --no-editable python main.py dimissione --contesto synthea-caso-02 --thinking
```

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

La richiesta unica restituisce le 17 sezioni in JSON con le fonti citate. Il codice
produce `bozza.txt`, `bozza_strutturata.json` ed `evidenze.md`, dove ogni sezione è
affiancata alle fonti selezionate dal modello, con origine, versione ed estratti
originali. In questa prima versione ogni evidenza copre una fonte completa. Il
catalogo è salvato prima dell'inferenza in `catalogo_evidenze.json`; lo stream
grezzo rimane in `risposta.parziale.json`, anche a completamento riuscito.
Riferimenti sconosciuti o incoerenti fanno fallire il controllo, conservando la
risposta grezza. Le citazioni sono per sezione e non certificano tutte le frasi.

Il template è `campus-discharge` 1.1.0: con istologico **No**, i dettagli restano
vuoti; se la risposta manca nelle fonti, viene segnalata la lacuna senza dedurre No.
I dettagli delle regole sono nei sorgenti del template e del prompt.

## Accessi fittizi locali

Gli endpoint di compilazione richiedono `Authorization: Bearer <token>`;
gli healthcheck restano pubblici. Credenziali assenti/invalide producono **401**,
casi fuori assegnazione **403**, anche nella selezione casuale.

| Identità | Token pubblico di prova | Casi |
|---|---|---|
| Medico A | `demo-medico-a` | Dispari |
| Medico B | `demo-medico-b` | Pari |
| Revisore | `demo-revisore` | Tutti i casi predisposti |

Il client usa il revisore per default; `APP_BACKEND_TOKEN` seleziona un altro
profilo. Sono identità dimostrative, senza login o servizi esterni.

```bash
APP_BACKEND_TOKEN=demo-medico-a uv run --locked --no-editable python main.py \
  dimissione --contesto synthea-caso-01 --dry-run --no-telemetry
```

Dopo modifiche al client/backend ricostruire le immagini mantenendo i dati:

```bash
docker compose build app backend
docker compose up -d --no-deps backend
```

## Osservabilità e verifiche

Lo stack, le configurazioni e la relativa guida sono raccolti in `observability/`.
Il `compose.yaml` principale include `observability/compose.yaml`: i comandi
restano eseguibili dalla radice con Docker Compose 2.20.0 o successivo. Registro,
metriche dello stream ed export sono nel package `src/codice_tesi/observability/`;
i relativi test sono in `tests/observability/`.

```bash
docker compose up -d --wait phoenix-db phoenix otel-collector prometheus
# Con backend e serving già disponibili:
docker compose run --rm app dimissione --contesto synthea-caso-02 --thinking \
  --experiment-file /app/experiments/exploratory.example.json
uv run --locked --no-editable python main.py registro --format csv \
  --destination output/metriche/esecuzioni.csv
```

Le interfacce locali sono [Phoenix](http://127.0.0.1:6006) e
[Prometheus](http://127.0.0.1:9090). Sul Mac configurare `observability/.env`
da `observability/.env.example` senza sovrascrivere impostazioni esistenti.
`--no-telemetry` disattiva l'export mantenendo il registro. I contenuti delle fonti e della bozza restano
negli artefatti locali e non sono esportati nelle tracce previste.

- [Osservabilità](observability/README.md): componenti, misure, uso e limiti.
- [Hardware Spark](observability/HARDWARE.md): sonda, integrazione per esecuzione e
  stato della verifica remota; attivazione sullo Spark ancora necessaria.
- [Sovranità](docs/sovranita.md): protocollo, accessi, conservazione e prove aperte.
- [Verifiche svolte](docs/verifiche-sovranita.md): risultati sintetici e perimetro.
- [Tracciabilità per sezione](docs/tracciabilita.md): contratto implementato, controlli
  e limiti; [esempio JSON didattico](docs/tracciabilita.esempio.json).

Log grezzi, richieste, copie e precedenti documenti generati sono conservati sotto
`output/`, esclusi da Git e dalle immagini. I verificatori accettano destinazioni
nuove; la cancellazione dimostrativa opera soltanto su copie censite, senza
pulizie programmate degli archivi correnti.

## Materiale sintetico e test

`scripts/build_discharge_dataset.py` costruisce una nuova cartella da esportazioni
Synthea (`--population`, `--output`) e può generare popolazioni con `--generate`,
`--java`, `--jar`. Mantiene gli originali e registra hash e regole delle aggiunte.
Il campione corrente è atteso nella cartella sorella
`../datasets_pubblici/synthea_dimissioni`, montata in sola lettura per l'import.

```bash
uv sync --locked --extra backend --no-editable
uv run --locked --extra backend --no-editable ruff check .
uv run --locked --extra backend --no-editable ruff format --check .
# CLINICAL_TEST_POSTGRES_URL deve indicare PostgreSQL di test.
uv run --locked --extra backend --no-editable pytest -q
```

I test SQL creano e rimuovono schemi isolati; senza la variabile di connessione
sono saltati. Le istruzioni per il DB temporaneo della conservazione sono nel
protocollo. Il progetto rimane una dimostrazione locale su dati sintetici.
