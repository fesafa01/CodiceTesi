# Backend clinico

Stato del 17 ottobre 2026: il vecchio dataset generato è stato rimosso.
Il backend conserva API di lettura, repository PostgreSQL, adattatore FHIR R4B
e strumenti di importazione, ma **non carica dati all'avvio**.

## Avvio

```bash
docker compose up --build -d backend
```

Compose avvia PostgreSQL, esegue `tesi-backend init` per creare le sole tabelle
e avvia il servizio con il ruolo `clinical_reader`. Nessun dataset viene montato
nei container o importato automaticamente. Il volume `clinical-db` conserva lo
schema e gli eventuali dati caricati esplicitamente.

- Swagger: <http://localhost:8001/docs>
- Liveness: `GET /health/live`
- Readiness del database inizializzato: `GET /health/ready`
- Elenchi iniziali: `GET /api/v1/reparti` e `GET /api/v1/ricoveri` restituiscono `[]`.

Nei metadati di un database vuoto `dataset_caricato=false`, revisione `0`,
scenario, esecuzione e versione del dataset sono `null`. Non viene creato uno
scenario fittizio. La readiness non attesta la presenza di pazienti o documenti.

## Comandi e importazione esplicita

```bash
uv run --locked --extra backend --no-editable tesi-backend init
uv run --locked --extra backend --no-editable tesi-backend serve
uv run --locked --extra backend --no-editable tesi-backend --dataset-dir /percorso/dataset validate-dataset
uv run --locked --extra backend --no-editable tesi-backend --dataset-dir /percorso/dataset import-dataset
```

`--dataset-dir` o `CLINICAL_DATASET_DIR` è richiesto solo per validazione e
importazione. Non esiste una directory predefinita. `init` crea le tabelle senza
importare dati anche quando la variabile è presente. `import-dataset` richiede
uno schema già inizializzato; l'importazione è atomica e non sostituisce un
dataset differente già presente. Il vecchio comando `seed` è stato rimosso.

Con Compose si può montare esplicitamente una directory esterna nel servizio
amministrativo su richiesta:

```bash
docker compose run --rm -v /percorso/dataset:/dataset:ro backend-control --dataset-dir /dataset validate-dataset
docker compose run --rm -v /percorso/dataset:/dataset:ro backend-control --dataset-dir /dataset import-dataset
```

Il loader supporta il contratto proprietario JSON/PDF descritto nella
[guida dei dati](dataset.md), non FHIR/CDA. Non è il futuro importatore HL7.

## API conservate

| Superficie | Comportamento |
| --- | --- |
| `/api/v1/reparti`, `/ricoveri` | Elenchi paginati e selezione del contesto. |
| `/ricoveri/{id}` | Ricovero e paziente associato. |
| `/ricoveri/{id}/fonti`, `/registrazioni`, `/documenti` | Filtri per episodio/paziente, canale, tipo, testo e date. |
| `/ricoveri/{id}/fonti/{fonte_id}` | Testo, sezioni, versione e provenienza. |
| `/ricoveri/{id}/documenti/{documento_id}/contenuto` | PDF conservato nel database, con ETag e versione esplicita. |
| `/ricoveri/{id}/risultati` | Misure numeriche collegate alla versione della fonte. |
| `/ricoveri/{id}/moduli`, `/modelli/{id}` | Copertura e modelli soltanto se importati. |
| `/ricoveri/{id}/aggiornamenti` | Variazioni fra revisioni di una stessa esecuzione importata. |
| `/ricoveri/{id}/fhir/metadata` | Capacità del sottoinsieme FHIR R4B, nel contesto di un ricovero esistente. |
| `/ricoveri/{id}/fhir/{tipo}` e `/{tipo}/{id}` | Search/read di Patient, Encounter, Observation, DiagnosticReport e DocumentReference. |

Il repository è utilizzabile senza HTTP tramite `open_repository()`, con una
transazione di sola lettura. Gli strumenti clinici non sono ancora collegati
all'agente. FHIR è una rappresentazione aggiuntiva dei dati SQL, non un convertitore
universale o un server certificato secondo profili italiani.

## Scenari esterni e test

Rimane il meccanismo generico di scenari e orologio per futuri esperimenti:
non sono distribuiti casi predefiniti. Lo scenario iniziale è dichiarato dal
campo `scenario_iniziale` del manifesto esterno; nessun identificativo è imposto.

```bash
docker compose run --rm backend-control scenario list
docker compose run --rm backend-control scenario status
```

Senza dataset la lista è vuota e la selezione/avanzamento viene rifiutata.
Dopo un'importazione si possono usare `scenario select <id>` e
`scenario advance --at <data-con-fuso>`.

```bash
CLINICAL_TEST_POSTGRES_URL=postgresql://utente:password@localhost:5433/database \
  uv run --locked --extra backend --no-editable pytest -q
uv run --locked --extra backend --no-editable ruff check .
uv run --locked --extra backend --no-editable ruff format --check .
```

I test creano uno schema PostgreSQL isolato per caso e fixture temporanee.
La CI fornisce PostgreSQL e non dipende da file clinici nella repository.
