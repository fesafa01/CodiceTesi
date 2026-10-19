# Backend documentale

Il backend serve il flusso **eCREAM → PostgreSQL → pacchetto di compilazione**.
L'applicazione riceve tutte le note del contesto insieme al template Campus,
poi genera la lettera senza accedere al database.

## Struttura

| File in `src/codice_tesi/backend/` | Responsabilità |
| --- | --- |
| `app.py` | Endpoint, errori HTTP, logging e controlli di salute. |
| `database.py` | `BackendSettings`, pool PostgreSQL e inizializzazione dello schema. |
| `repository.py` | Importazione atomica di contesti/fonti e lettura del pacchetto. |
| `importer.py` | Lettura e raggruppamento eCREAM, coordinamento del salvataggio. |
| `cli.py` | Comandi `init`, `import-ecream` e `serve`. |
| `schema.sql` | Le sole tabelle `composition_contexts` e `composition_sources`. |

I modelli del pacchetto e il template canonico sono condivisi con l'applicazione
nel modulo `discharge`; non esistono copie specifiche per il backend.

## Avvio e importazione

```bash
docker compose up --build -d backend
docker compose run --rm \
  -v "$PWD/../datasets_pubblici/ecream_gamma_span:/dataset:ro" \
  backend-admin import-ecream /dataset/notes.jsonl
```

Compose avvia PostgreSQL, esegue `backend-admin init` e avvia il backend con il
ruolo `clinical_reader`, di sola lettura. Lo stesso servizio amministrativo
viene riutilizzato per l'importazione esplicita. Il volume `clinical-db`
conserva lo schema e le note; nessun dataset viene caricato all'avvio.

L'avvio locale usa la stessa CLI:

```bash
uv run --locked --extra backend --no-editable tesi-backend init
uv run --locked --extra backend --no-editable tesi-backend import-ecream /percorso/notes.jsonl
uv run --locked --extra backend --no-editable tesi-backend serve
```

Le impostazioni mantengono il prefisso `CLINICAL_`: `DATABASE_URL`,
`DATABASE_SCHEMA`, `HOST`, `PORT`, `POOL_MIN_SIZE`, `POOL_MAX_SIZE`,
`POOL_TIMEOUT` e `LOG_LEVEL`. L'URL locale predefinito punta alla porta 5433,
mentre l'API ascolta su `127.0.0.1:8001`.

## API

Swagger è disponibile su <http://localhost:8001/docs>.

| Endpoint GET | Comportamento |
| --- | --- |
| `/api/v1/compilazione/template` | Template Campus canonico, anche senza note importate. |
| `/api/v1/compilazione/avvio-test?seed=42` | Seleziona un contesto in modo riproducibile. |
| `/api/v1/compilazione/contesti/{id}/pacchetto` | Restituisce il contesto richiesto e tutte le sue fonti. |
| `/health/live` | Processo attivo. |
| `/health/ready` | Database raggiungibile e due tabelle documentali leggibili. |

La readiness accetta un database inizializzato ma vuoto. In assenza di note,
`avvio-test` restituisce `409`; un contesto inesistente restituisce `404`.
Un seed negativo o non intero restituisce `422`.
Gli errori usano `application/problem+json` e includono il `request_id`, esposto
anche nell'header `X-Request-ID`. I log non includono i testi delle fonti.

Il pacchetto resta nello schema `3.0`, con `context`, un unico elenco `sources`
ordinato e `template`. Contesto e fonti vengono letti nella stessa transazione
`REPEATABLE READ READ ONLY`; non c'è selezione delle note per campo della lettera.
Il template resta `campus-discharge` versione `1.0.0`.

## Compatibilità dei dati

I database esistenti conservano i contesti eCREAM già importati: le due tabelle
documentali e i relativi payload restano invariati. `init` è idempotente e crea
soltanto le tabelle mancanti. Le vecchie tabelle del simulatore eventualmente
presenti restano inutilizzate e non vengono cancellate automaticamente.

Sono stati rimossi il simulatore clinico, gli scenari, le API di ricoveri,
fonti cliniche e FHIR, il loader JSON/PDF e i relativi comandi. In Compose
`backend-admin` sostituisce i precedenti servizi `backend-init` e `backend-control`.
Il nome del volume PostgreSQL resta invariato.

## Verifiche

```bash
CLINICAL_TEST_POSTGRES_URL=postgresql://utente:password@localhost:5433/database \
  uv run --locked --extra backend --no-editable pytest -q
uv run --locked --extra backend --no-editable ruff check .
uv run --locked --extra backend --no-editable ruff format --check .
```

I test creano uno schema PostgreSQL isolato per caso e note temporanee.
La CI fornisce PostgreSQL e non dipende da file clinici nella repository.
Per il formato di importazione vedere il [contratto eCREAM](dataset.md).
