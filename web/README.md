# Interfaccia dimissioni

Servizio Next.js con React, TypeScript, Tailwind CSS e React Icons, nelle stesse
versioni utilizzate dalla cartella di riferimento `tcs-assistant`. Bun gestisce dipendenze e runtime;
`bun.lock` fissa la risoluzione. Non richiede servizi cloud.

Dalla radice di CodiceTesi:

```bash
docker compose up -d --build web
```

Aprire [http://localhost:3000](http://localhost:3000). Il comando aggiorna anche
backend e agent-api e riusa il volume PostgreSQL. L’import del campione MIMIC e
le credenziali per l'accesso locale sono descritti nel [README principale](../README.md).

## Flusso

1. La prima pagina legge `GET /api/v1/compilazione/contesti`: i casi provengono
   da PostgreSQL e sono filtrati per le assegnazioni dell'identità locale.
2. “Continua” chiama agent-api per recuperare il pacchetto della degenza e
   conservarne uno snapshot. Nella seconda pagina compaiono le 17 sezioni Campus,
   gli identificativi verificati e tutte le fonti disponibili. In fondo alla
   pagina, **Lettera di dimissione originale** apre il riferimento MIMIC-IV-Note
   per il confronto. Il testo è caricato all'apertura, fuori dallo snapshot.
3. Ogni sezione ha un editor di testo, utilizzabile prima, durante e dopo la
   generazione. **Salva modifiche** conserva i testi sulla preparazione; prima di
   **Genera** le modifiche presenti vengono salvate anche automaticamente.
4. “Genera” avvia `run_application` sullo stesso pacchetto già caricato, senza
   rileggere fonti eventualmente cambiate nel DB. Il testo appare progressivamente
   nelle rispettive sezioni, anche prima che il JSON sia completo. Il lavoro
   continua chiudendo la pagina; riaprendo l'URL si recuperano stato, anteprima
   e modifiche salvate.
5. Dopo i controlli esistenti su struttura, campi e riferimenti, la bozza viene
   mostrata con le citazioni della sezione selezionata. Ogni fonte si apre come
   documento impaginato, anche in una scheda dedicata: campi strutturati, eventi
   della degenza e ricerca nel contenuto. Le viste Testo originale e JSON
   conservano il dettaglio completo, con download del JSON e metadati di provenienza.
   Il download TXT contiene il testo attualmente nell'editor, comprese le
   modifiche manuali; se la generazione non è completata, lo dichiara nel titolo.

Le citazioni sono quelle proposte dal modello e validate nei riferimenti: la
correttezza clinica rimane da revisionare. Prima della generazione il catalogo
mostra tutte le fonti disponibili, senza attribuirle arbitrariamente alle sezioni.
Il testo delle sezioni è modificabile; intestazioni e struttura del template
restano canoniche. La UI non gestisce firma o approvazione clinica.

Le modifiche manuali prevalgono sullo streaming, anche quando il testo inserito
è vuoto. **Usa testo AI** rimuove la modifica della singola sezione e ripristina
il testo generato più recente. Quando una sezione è stata modificata, un riquadro
espandibile mantiene visibile il testo AI per confronto. I testi manuali non
vengono inseriti nel prompt: il modello lavora sul pacchetto delle fonti originale.
Le citazioni mostrate riguardano esclusivamente la versione generata dal modello.
L'originale validato e i suoi hash non vengono riscritti dalle modifiche manuali.

Lo streaming usa SSE attraverso il proxy Next.js, senza buffering o scadenza
totale della connessione, con aggiornamenti dell'anteprima fino a quattro volte
al secondo. Se la connessione live non è disponibile, la pagina torna alla lettura
periodica dello stato ogni 2,5 secondi. I frammenti sono sempre indicati come
provvisori fino al completamento dei controlli finali; ragionamento e argomenti
dei tool non sono inclusi nell'anteprima. Una nuova risposta nel ciclo con tool
sostituisce l'anteprima precedente, conservando i testi manuali.

Prima della generazione si possono spuntare separatamente **Reasoning** e
**Verifiche documentali**: tutte e quattro le combinazioni sono supportate dalla UI.
La casella **Verifiche documentali** abilita riepiloghi delle osservazioni,
sequenze farmacologiche e cronologie sullo snapshot, oltre alla lettura puntuale
delle evidenze. Tutto il contesto è già inviato nella prima richiesta.
I valori iniziali sono reasoning attivo e tool disattivi. Le opzioni scelte
restano visibili durante e dopo il lavoro e sono conservate nello stato.
Non c'è una durata massima complessiva predefinita; restano i timeout di
inattività e connessione. Durante il lavoro sono
visibili fase, tempo trascorso e pulsante **Interrompi**. Il clic sulle citazioni
apre il passaggio richiamato; dati strutturati e rilievi documentali sono
consultabili nella colonna delle fonti. L'arresto remoto sullo Spark resta da
verificare separatamente. Dettagli nelle
[integrazioni agentiche](../docs/integrazioni-agentiche.md).

## Servizi e configurazione

- `web` (porta locale 3000): UI e proxy HTTP sullo stesso origin.
- `agent-api` (porta interna 8002): preparazione, generazione e stato.
- `backend` (porta 8001): lettura dei dati clinici dal DB.
- `reference-api`: servizio di sola lettura per le lettere originali, su una
  rete Docker interna raggiungibile da `web`.

In modalità MIMIC, web e API richiedono la credenziale del file privato
`var/mimic/access.env`. Next.js richiede l'accesso Basic e inoltra alle API il
token Bearer.
Non sono presenti credenziali LLM nel browser. Le altre variabili server sono
`APP_BACKEND_URL`, `AGENT_API_URL` e `REFERENCE_API_URL`. agent-api legge la configurazione LLM e
l’osservabilità già usate dal client CLI.

Snapshot e stati web sono in `output/dimissioni-mimic/web-sessions/<uuid>/`
per MIMIC.
`preview.json` conserva l'ultima anteprima e `editor.json` i testi manuali con
numero di revisione. Un salvataggio da una scheda non aggiornata riceve 409,
senza sovrascrivere le modifiche dell'altra scheda. I testi manuali non sono
esportati in telemetria. Le
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
Per verificare la consultazione delle lettere originali, usare `web` in Docker
Compose, dove Next.js raggiunge `reference-api` sulla rete interna.

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
