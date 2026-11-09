# CodiceTesi 0.2.0

Applicazione locale per preparare e revisionare una bozza di lettera di dimissione
nei 17 campi CAMPUS. Comprende interfaccia web, backend PostgreSQL, generazione
in streaming, fonti consultabili, editor e archivio delle prove. La bozza richiede
la revisione del medico; i controlli automatici verificano struttura e citazioni.

## Download e requisiti

Scaricare `CodiceTesi-0.2.0.zip` dalla
[release 0.2.0](https://github.com/fesafa01/CodiceTesi/releases/tag/v0.2.0)
oppure clonare la repo. L'archivio contiene i sorgenti e le configurazioni Docker;
non contiene dataset, lettere, credenziali o pesi dei modelli.

Servono Docker con Compose 2.24 o successivo, un endpoint LLM compatibile con
OpenAI e, per usare il flusso MIMIC, accesso autorizzato a MIMIC-IV Hosp,
MIMIC-IV-Note e MIMIC-IV-ED. Il modello viene eseguito sul proprio server;
l'applicazione non scarica pesi. Per il DGX Spark del progetto serve Tailscale.

## Configurazione e avvio

Dalla cartella estratta, copiare `.env.example` in `.env` se il file non esiste
e configurare modello ed endpoint. `LLM_OLLAMA_BASE_URL` deve essere raggiungibile
**dai container**: con Ollama sul computer locale usare
`http://host.docker.internal:11434/v1`. Il default del progetto punta allo Spark.

I dati restano in una cartella privata esterna alla repo. Impostare
`TESI_PRIVATE_DIR` con un percorso assoluto se non si usa la cartella sorella
`../codice-tesi-private`. La struttura attesa è:

```text
codice-tesi-private/datasets/
  hosp/                                      # CSV compressi MIMIC-IV
  mimic-iv-note-deidentified-free-text-clinical-notes-2.2/note/
  mimic-iv-ed-2.2/ed/
```

Al primo avvio, importare esplicitamente il campione autorizzato:

```bash
export TESI_PRIVATE_DIR="${TESI_PRIVATE_DIR:-$(cd .. && pwd)/codice-tesi-private}"
install -d -m 700 "$TESI_PRIVATE_DIR/var/mimic" \
  "$TESI_PRIVATE_DIR/output" "$TESI_PRIVATE_DIR/TEST-SPARK-QWEN"
docker compose build backend-admin
docker compose up -d --wait db
docker compose run --rm --user "$(id -u):$(id -g)" \
  -v "$TESI_PRIVATE_DIR/datasets/hosp:/mimic/hosp:ro" \
  -v "$TESI_PRIVATE_DIR/datasets/mimic-iv-note-deidentified-free-text-clinical-notes-2.2/note:/mimic/note:ro" \
  -v "$TESI_PRIVATE_DIR/datasets/mimic-iv-ed-2.2/ed:/mimic/ed:ro" \
  -v "$TESI_PRIVATE_DIR/var/mimic:/mimic-private" \
  backend-admin import-mimic /mimic/hosp /mimic/note \
    --ed /mimic/ed --hosp-ed-only --limit 30 --seed 42 \
    --max-input-tokens 80000 --replace --access-file /mimic-private/access.env
docker compose up -d --build --wait backend agent-api web
```

`--replace` sostituisce il campione MIMIC nel database. Per gli avvii successivi
basta l'ultimo comando. Senza importazione il database è vuoto. Aprire
[CAMPUS](http://127.0.0.1:3000), utente `mimic`, password `APP_MIMIC_TOKEN` del
file privato `var/mimic/access.env`. Le API verificano token e assegnazioni.
Dopo un nuovo import riavviare `backend reference-api agent-api spark-archive web`.

Nell'interfaccia: scegliere il caso, preparare le fonti, impostare **Reasoning**
e **Verifiche documentali**, quindi generare. Le modifiche manuali si salvano e
sono incluse nel download; testo AI e citazioni restano consultabili. Riaprendo
la pagina si recupera il lavoro. È supportata una generazione alla volta.

Modelli preconfigurati: `qwen3.8:27b`, `gemma4:31b`,
`frob/mistral-small-4:119b` via Ollama; `deepseek-v4-flash` usa `LLM_BASE_URL`.
Il livello di reasoning è low, high per Mistral. `LLM_TIMEOUT` e
`LLM_FIRST_TEXT_TIMEOUT` configurano l'inattività; per Gemma/Mistral si usa
`LLM_GEMMA_MISTRAL_TIMEOUT` (600 secondi). Verificare sul serving una finestra
sufficiente per input, output e tool. Dopo modifiche a `.env` ricreare `agent-api`.

## Prove e dati privati

[Risultati](risultati/README.md) raccoglie le cartelle delle prove con relativi
protocolli e riepiloghi tecnici. I test automatici in `tests/` e `web/tests/`
usano fixture inventate.

Gli archivi completi `TEST-SPARK-*`, i dataset, gli input, le lettere originali,
le bozze, le evidenze e i checkpoint restano in `TESI_PRIVATE_DIR`. La release e
Git escludono questi materiali. `.gitignore` non protegge file già tracciati:
prima della pubblicazione usare anche il verificatore della distribuzione.

Lo stack scrive gli artefatti in `output/dimissioni-mimic/` nella cartella privata
e archivia i tentativi in `TEST-SPARK-QWEN/`; impostare `TESI_SPARK_ARCHIVE_DIR`
per un altro archivio. Ogni prova conserva scheda, metriche, fonti, bozza,
originale e risultati tool disponibili, anche in caso di errore o interruzione.

Per le campagne, usare un archivio nuovo e un checkpoint dedicato:

```bash
python3 scripts/prepare_campaign_archive.py "$TESI_PRIVATE_DIR/TEST-SPARK-QWEN-TOOLS-v2"
LLM_MODEL=qwen3.8:27b TESI_SPARK_ARCHIVE_DIR=TEST-SPARK-QWEN-TOOLS-v2 \
  docker compose up -d --build --wait agent-api spark-archive web
python3 scripts/run_qwen_campaign.py --suite reasoning \
  --state "$TESI_PRIVATE_DIR/campagna-qwen-reasoning-tools-v2.json"
```

I runner Gemma e Mistral seguono lo stesso schema. Non riutilizzare gli archivi
delle campagne concluse. `--report` legge il checkpoint; il report aggregato
si genera con `scripts/report_reasoning_suite.py ARCHIVIO...`.

La telemetria MIMIC è disattivata. Phoenix e Prometheus locali sono su
[6006](http://127.0.0.1:6006) e [9090](http://127.0.0.1:9090).
Le misure Spark sono acquisite automaticamente a fine prova quando il monitor
è disponibile; stime e motivi di indisponibilità restano nelle metriche.
`observability/spark_usage.py` consente la raccolta manuale;
`spark_recover_energy.py` ricalcola le stime dagli snapshot privati.

## Verifica e distribuzione

Per lo sviluppo servono Python 3.13, uv 0.11.19 e Bun 1.3.13:

```bash
uv sync --locked --extra backend --no-editable
uv run --locked --extra backend --no-editable ruff check .
uv run --locked --extra backend --no-editable ruff format --check .
uv run --locked --extra backend --no-editable pytest -q
cd web
bun install --frozen-lockfile
bun run test
bun run build
bun run typecheck
```

I test SQL richiedono `CLINICAL_TEST_POSTGRES_URL` verso un database di test
separato; creano e rimuovono schemi isolati. Senza connessione vengono saltati.
Per sviluppare Next.js si può usare `web/compose.dev.yaml` come override di Compose.

Dalla radice, dopo il commit, `python3 scripts/check_distribution.py --ref HEAD`
verifica i file da distribuire. Con `--output dist/CodiceTesi-0.2.0.zip` genera
l'archivio e il relativo SHA-256 usando soltanto i file Git della revisione.
La CI esegue gli stessi controlli; il workflow Release li ripete sul tag prima
di allegare il pacchetto a GitHub.
