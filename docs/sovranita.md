# Verifica della sovranità nel prototipo locale

Il prototipo produce una bozza da fonti documentali, con inferenza sullo Spark e revisione affidata al medico. Qui sono definiti i controlli tecnici, le prove e i limiti del perimetro locale. Identità e politica di conservazione sono dimostrative; i collaudi usano materiale sintetico provvisorio. Non si attribuisce a questi dati il ruolo di dataset della valutazione clinica.

## Requisiti e stato

| Requisito | Presente e verificabile | Prova | Che cosa resta aperto |
|---|---|---|---|
| Controllo dei dati | Contesto e hash delle fonti validati, ruolo DB di lettura, accessi demo, scadenze su copie censite. | **D1:** integrità/perimetro/log. **D2:** accessi e ciclo di vita. | Inventario completo di copie, cache, log e backup del serving e dei servizi attivi. |
| Autonomia in esercizio | Servizi locali e collegamento allo Spark configurati. | **A1:** avvio, inferenza e riavvio con uscita esterna bloccata su entrambi gli host. | Prova di rete completa; endpoint locali e dry-run non dimostrano autonomia offline. |
| Identità e ripristino | Lock Python, hash di codice e richieste, metadati delle esecuzioni. | **R1:** ricostruzione isolata da un archivio interno. | Manifest effettivo di pesi/tokenizer/runtime, licenze, immagini e ripristino completo. |
| Qualità documentale | Template, controlli delle intestazioni/istologico e del budget. | **Q1:** invarianti su sintetici, copertura e affermazioni supportate. | Annotazioni semantiche e valutazione clinica; il formato valido non prova correttezza. |
| Contenimento | Nessun tool del modello; accesso in lettura; parziali e fallimenti conservati. | **C1:** contratti/privilegi. **C2:** istruzioni spurie e arresto del serving. | Prove sul modello e conferma del rilascio delle risorse sullo Spark. |
| Portabilità | Endpoint e modello configurabili; contratto comune. | **P1:** sostituzione controllata e nuova valutazione. | Secondo checkpoint/runtime, flag adattati e confronto di qualità/costo. |
| Sostenibilità | Registro di tempi, usage quando disponibile e fallimenti. | **S1:** carico/risorse. **S2:** indisponibilità/recupero. | Misure hardware e prove di servizio reale, oltre ai mock. |

Gli esiti delle singole prove sono `SUPERATA`, `NON_SUPERATA`, `PARZIALE`, `NON_ESEGUITA` o `NON_VALUTABILE`. Un test con risposte simulate non attesta il comportamento del modello o dell'infrastruttura reale. Le [verifiche effettuate](verifiche-sovranita.md) distinguono i risultati osservati da questi obiettivi.

## Accessi fittizi

Tutti gli endpoint `/api/v1/compilazione/*`, incluso il template, richiedono un Bearer token di prova. Gli healthcheck restano pubblici sul servizio locale.

| Identità | Token pubblico | Assegnazione |
|---|---|---|
| Medico A | `demo-medico-a` | Casi sintetici dispari |
| Medico B | `demo-medico-b` | Casi sintetici pari |
| Revisore | `demo-revisore` | Tutti i casi predisposti nella demo |

Credenziali assenti o invalide ricevono **401** con challenge Bearer. Un contesto fuori assegnazione riceve **403**, prima di interrogarne l'esistenza. Il selettore casuale filtra i candidati in SQL e non ripiega su casi altrui. Il client usa per default il revisore; `APP_BACKEND_TOKEN` cambia il profilo. I token non vengono salvati in richieste, registri o tracce; il log backend registra il soggetto fittizio autenticato.

```bash
# Dalla cartella del repository, con backend e dati sintetici già disponibili.
PYTHONPATH=src .venv/bin/python scripts/verify_local_access.py \
  --output output/verifiche/nuova-sessione/accessi.json
```

Il verificatore definisce gli esiti attesi separatamente dalla mappa del backend: richieste anonime/invalide, casi consentiti/negati e selezione con più seed. I token sono intenzionalmente noti: la prova verifica l'applicazione della policy alla credenziale presentata, non l'identità di una persona reale. Non è previsto un servizio di identità esterno.

## Conservazione e cancellazione dimostrative

Conservazione significa stabilire quali copie tenere e per quanto tempo. Cancellazione significa applicarne la scadenza e controllare file, righe SQL e backup inclusi. La rimozione di una bozza non elimina automaticamente la fonte.

La politica `demo-1.0` si applica solo a nuove copie censite. Le date sono UTC dichiarate nel catalogo; il collaudo simula il passare del tempo senza modificare l'orologio del computer. Alla scadenza esatta la copia è eliminabile. Letture e ripristini non prolungano la conservazione.

| Categoria della sandbox | Durata di prova | Azione |
|---|---|---|
| Fonti su file e contesti/fonti SQL | 7 giorni dalla creazione della copia | Rimozione dei file e delle righe censiti. |
| Richieste, bozze e parziali | 24 ore | Rimozione degli artefatti censiti. |
| Fixture su file di log/tracce/metriche/cache | 24 ore | Rimozione delle fixture. |
| Registri tecnici senza corpo delle note | 30 giorni | Rimozione dei record censiti. |
| Backup della fonte | Al massimo 7 giorni, mai oltre la fonte | Rimozione e rifiuto del ripristino da scaduto. |
| Catalogo e verbale di verifica | Documentazione locale, fuori dal TTL delle copie | Conservazione di metadati ed esiti, senza corpo di note o bozze. |

Un backup creato al sesto giorno non dà altri sette giorni alla fonte. La copia ripristinata eredita la scadenza del backup. Le durate sono parametri della dimostrazione, non termini per cartelle cliniche o obblighi ospedalieri.

La utility richiede una cartella nuova e marcata, rifiuta link simbolici, percorsi esterni e file non censiti. La cancellazione SQL è limitata allo schema `retention_demo_<id>` corrispondente alla sandbox e agli ID registrati, su una connessione locale. Un DB errato interrompe l'applicazione prima di rimuovere i file. L'anteprima non modifica le copie; l'applicazione completata è ripetibile senza ulteriori rimozioni.

Per la prova completa, dalla cartella del repository, con immagine già presente:

```bash
docker run --detach --rm --pull=never --name tesi-retention-demo \
  --publish 127.0.0.1:15435:5432 \
  --env POSTGRES_PASSWORD=retention_test_locale --env POSTGRES_DB=retention_test \
  --tmpfs /var/lib/postgresql/data postgres:17-bookworm
docker exec tesi-retention-demo pg_isready -U postgres -d retention_test
```

Quando il DB è pronto, scegliere una destinazione nuova:

```bash
PYTHONPATH=src \
  RETENTION_DEMO_POSTGRES_URL=postgresql://postgres:retention_test_locale@127.0.0.1:15435/retention_test \
  .venv/bin/python scripts/verify_local_retention.py \
  --output output/verifiche/nuova-sessione-conservazione
docker stop tesi-retention-demo
```

La prova crea copie derivate di due contesti e un controllo fuori inventario; verifica anteprima, scadenze iniziali/24 ore/7 giorni/30 giorni, ripristino valido/scaduto e idempotenza. Controlla gli hash degli originali e rimuove il proprio schema temporaneo. Richieste, bozze e telemetria sono fixture create per il test; non viene eseguita inferenza.

La cancellazione osservata è **logica**, non sovrascrittura fisica di SSD, WAL o snapshot. Non esiste una transazione unica fra DB e filesystem: un errore può lasciare uno stato parziale da controllare. Gli archivi ordinari e i servizi attivi non vengono ripuliti. Prometheus mantiene la retention configurata di 90 giorni; Phoenix, coda del Collector, log Docker e serving richiedono regole proprie. D2 sull'intero ambiente rimane quindi parziale.

## Procedure per le prove aperte

- **D1:** confrontare hash e identità dei pacchetti; cercare marcatori sintetici distinti negli export reali di tracce, metriche e log. Includere cache, terminali e backup. Gli identificativi dei registri e gli hash non rendono anonimo l'ambiente.
- **A1:** predisporre artefatti e accesso di recupero; bloccare l'uscita esterna mantenendo la rete interna; generare e riavviare i processi su entrambi gli host. Conservare regole effettive, tentativi di connessione e run ID. Un processo già caldo non dimostra avvio autonomo.
- **R1:** archiviare pesi, tokenizer, runtime, immagini, pacchetti, configurazioni e licenze; ricostruire su istanza e volumi nuovi con rete controllata. Verificare hash, healthcheck, recupero, generazione e tempi. Non richiedere identità byte per byte di output stocastici.
- **Q1:** annotare prima degli output fatti, fonte e tempo; affiancare al formato copertura, negazioni e affermazioni non supportate. Separare lacune delle fonti, errori di estrazione e di generazione. Tenere sviluppo e test separati; soglie cliniche da concordare con i medici.
- **C2:** preparare note pulite/manipolate in un backend separato, con hash coerenti; confrontare rispetto del compito e connessioni. Interrompere una generazione e osservare anche l'arresto sullo Spark: il solo parziale client non basta.
- **P1:** ripetere gli stessi casi su un'alternativa locale, documentando flag e adattamenti. Distinguere cambio di modello e motore; la compatibilità HTTP non garantisce qualità equivalente.
- **S1/S2:** identificare riscaldamento e cache, misurare carico e risorse sullo Spark, includere fallimenti nel denominatore. Provare l'indisponibilità di una componente per volta in istanza separata e registrare recupero e qualità residua.

Per ogni prova conservare versione del protocollo, operatore, tempi UTC, ambiente (mock/backend/Spark), configurazione e hash, casi/ripetizioni, procedura, criterio fissato prima della prova, osservazioni, stato, run ID e limiti. I campi sconosciuti restano `null`/`unknown`. La qualità tecnica non sostituisce la valutazione clinica.

## Evidenze e manutenzione

I risultati sintetici sono nel [registro delle verifiche](verifiche-sovranita.md); tracce e metriche sono descritte in [osservabilità](../observability/README.md). I dettagli locali sono sotto `output/verifiche/`, esclusi da Git e dalle immagini. Non pubblicare richieste complete o dump di credenziali per documentare una prova.

Le modifiche a modello, runtime, rete, accessi o logging richiedono di rivalutare le prove dipendenti. Questo protocollo documenta un perimetro di controllo; non è una certificazione di conformità né un risultato di efficacia clinica.
