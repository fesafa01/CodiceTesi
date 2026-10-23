# CodiceTesi

Bozza Campus dai 30 ricoveri sintetici Synthea FHIR R4. Il contesto comprende solo
la degenza selezionata e l'anamnesi riportata nelle sue note; gli originali restano
in `../datasets_pubblici/synthea_dimissioni`.

Con Docker, Tailscale e server LLM attivi, dalla cartella del progetto:

```bash
docker compose up --build -d backend
# Solo per inizializzare o aggiornare le fonti del campione:
docker compose run --rm backend-admin import-synthea /dataset --replace
uv run --locked --no-editable python main.py dimissione --contesto synthea-caso-23 --thinking
```

Una sola richiesta produce la lettera. `--thinking` abilita il reasoning nella
stesura; `--no-thinking` lo disabilita. Senza flag vale `ModelConfig.thinking`
in `src/codice_tesi/config.py` (predefinito: attivo).
`max_tokens` comprende ragionamento e testo; `first_text_timeout` limita l'attesa
del primo testo. La connessione usa `LLM_BASE_URL`, `LLM_API_KEY` e `LLM_TIMEOUT`.

Il prompt è in `src/codice_tesi/discharge/prompts.py`; i campi canonici sono in
`src/codice_tesi/discharge/templates/campus_discharge.v1.json`.
`--dry-run` salva la richiesta senza inferenza. I risultati vanno in
`output/dimissioni/<contesto>/<esecuzione>/`: richiesta, bozza ed esito tecnico.
Ctrl+C o un errore conservano il parziale. Il reasoning interno non viene salvato.
La bozza richiede revisione medica.

Il registro versionato in `output/dimissioni/registro/<run_id>.json` raccoglie anche
dry-run, fallimenti prima dell'inferenza e interruzioni. Include tempi per fase,
primo chunk/primo testo, token disponibili, hash e configurazione sperimentale.
I contatori non esposti dal server restano `null`.

Lo stack locale usa container separati per Phoenix, PostgreSQL dedicato,
OpenTelemetry Collector e Prometheus:

```bash
docker compose up -d --wait phoenix-db phoenix otel-collector prometheus
docker compose build app
docker compose run --rm app dimissione --contesto synthea-caso-02 --thinking \
  --experiment-file /app/experiments/exploratory.example.json
```

[Phoenix](http://127.0.0.1:6006) visualizza le tracce;
[Prometheus](http://127.0.0.1:9090) conserva le metriche aggregate.
Per l'esecuzione Python sul Mac, copiare `.env.observability.example` in
`.env.observability` senza sovrascrivere configurazioni esistenti.
Il client Docker è già configurato per esportare al Collector.
`--no-telemetry` disabilita l'export, mantenendo il registro locale.
Prompt, testi clinici e reasoning non vengono esportati nelle tracce.

```bash
uv run --locked --no-editable python main.py registro --format csv \
  --destination output/metriche/esecuzioni.csv
```

Architettura, definizioni delle misure, metadati sperimentali, persistenza e limiti
sono in [docs-osservabilita.md](docs-osservabilita.md).

```bash
CLINICAL_TEST_POSTGRES_URL=postgresql://clinico:clinico_sintetico@127.0.0.1:5433/clinico \
  uv run --locked --extra backend --no-editable pytest -q
uv run --locked --extra backend --no-editable ruff check .
```
