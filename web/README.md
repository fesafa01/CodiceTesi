# Interfaccia dimissioni

Servizio Next.js con React, TypeScript, Tailwind CSS e React Icons, nelle stesse
versioni utilizzate dalla cartella di riferimento `tcs-assistant`. Bun gestisce dipendenze e runtime;
`bun.lock` fissa la risoluzione. Non richiede servizi cloud.

Dalla radice di CodiceTesi:

```bash
docker compose up -d --build web
```

Aprire [http://localhost:3000](http://localhost:3000). Il comando aggiorna anche
backend e agent-api; mantiene il volume PostgreSQL esistente. L’import del dataset
resta esplicito, come descritto nel README principale.

## Flusso

1. La prima pagina legge `GET /api/v1/compilazione/contesti`: i casi provengono
   da PostgreSQL e sono filtrati per le assegnazioni dell’identità demo.
2. “Continua” chiama agent-api per recuperare il pacchetto della degenza e
   conservarne uno snapshot. Nella seconda pagina compaiono le 17 sezioni Campus,
   gli identificativi verificati e tutte le fonti disponibili.
3. “Genera” avvia `run_application` sullo stesso pacchetto già caricato, senza
   rileggere fonti eventualmente cambiate nel DB. Il lavoro continua anche
   chiudendo la pagina. L’URL della preparazione consente di recuperare lo stato.
4. Dopo i controlli esistenti su struttura, campi e riferimenti, la bozza viene
   mostrata con le citazioni della sezione selezionata. Ogni fonte si apre come
   documento impaginato, anche in una scheda dedicata: campi strutturati, eventi
   della degenza e ricerca nel contenuto. Le viste Testo originale e JSON
   conservano il dettaglio completo, con download del JSON e metadati di provenienza.
   È disponibile il download della bozza TXT.

Le citazioni sono quelle proposte dal modello e validate nei riferimenti: la
correttezza clinica rimane da revisionare. Prima della generazione il catalogo
mostra tutte le fonti disponibili, senza attribuirle arbitrariamente alle sezioni.
Il template resta in sola lettura; la UI non gestisce firma o approvazione clinica.

## Servizi e configurazione

- `web` (porta locale 3000): UI e proxy HTTP sullo stesso origin.
- `agent-api` (porta interna 8002): preparazione, generazione e stato.
- `backend` (porta 8001): lettura dei dati clinici dal DB.

Il token demo è solo sul server Next.js, configurato con `APP_BACKEND_TOKEN`
(default `demo-revisore`). Cambiare token richiede di ricreare il servizio web.
Non sono presenti credenziali LLM nel browser. Le altre variabili server sono
`APP_BACKEND_URL` e `AGENT_API_URL`. agent-api legge la configurazione LLM e
l’osservabilità già usate dal client CLI.

Snapshot e stati web sono in `output/dimissioni/web-sessions/<uuid>/`; le
esecuzioni mantengono gli artefatti e il registro standard. Il riavvio conserva
preparazioni e risultati, marcando come interrotti i lavori non terminati. Nessun
retry automatico: dopo errore o interruzione si prepara nuovamente la degenza.

Il prototipo esegue **una generazione alla volta** e richiede un singolo worker
agent-api (come configurato). Un secondo avvio riceve 409 e può essere riprovato
al termine. POST ripetuti sulla stessa preparazione non duplicano l’inferenza.
Non c’è una coda distribuita né cancellazione automatica degli artefatti.

## Sviluppo

Con backend e agent-api avviati, si può sviluppare il frontend sul Mac:

```bash
# Espone agent-api solo in loopback, usando l’override opzionale:
docker compose -f compose.yaml -f web/compose.dev.yaml up -d agent-api
cd web
bun install --frozen-lockfile
bun run dev
```

L’override espone 8002 solo per lo sviluppo; in alternativa eseguire localmente
`PYTHONPATH=src uv run --locked --extra backend python -m codice_tesi.web_api`.
Non avviare contemporaneamente due agent-api sulla stessa cartella di output.
Per evitare conflitti di porta con il web Docker, fermarlo prima (`docker compose
stop web`) oppure avviare Next su un’altra porta (`bun run dev --port 3001`).

```bash
bun run build
bun run typecheck
bun run test
# Dalla radice:
PYTHONPATH=src uv run --locked --extra backend pytest tests/web_api -q
```

Il deploy usa la modalità standalone documentata da
[Next.js](https://nextjs.org/docs/app/guides/self-hosting). Il proxy evita la
cache dei contenuti e inoltra solo le rotte applicative previste.
