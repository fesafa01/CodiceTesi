# Evidenze, controllo dell'esecuzione e verifiche documentali

Il percorso ordinario redige una bozza con una sola richiesta al modello.
I tool opzionali elaborano lo stesso snapshot autorizzato, con
budget complessivi e tracciamento dei tentativi.

Per editor, streaming e uso dell'interfaccia vedere [web/README.md](../web/README.md).
Per avvio e configurazione vedere il [README principale](../README.md).
I valori predefiniti sono definiti in `src/codice_tesi/config.py` e salvati nel
registro di ogni esecuzione, senza duplicarli in questa guida.

## Evidenze originali

Il catalogo identifica la segmentazione come `passaggi-v1`:

- per `dati_ricovero`, ogni riga dell'adattatore rappresenta un evento;
- le note sopra 1.200 caratteri vengono separate ai confini dei paragrafi;
- le note brevi e i paragrafi non divisibili rimangono interi.

Non si spezzano arbitrariamente frasi o JSON annidati. Gli intervalli sono
punti di codice Unicode nel testo originale, senza normalizzazione. Date,
negazioni e attributi contenuti nel record restano insieme. Il contesto della
degenza resta disponibile nella fonte tecnica e nel pacchetto verificato.

Il prompt associa ciascun intervallo al proprio ID e conserva tutti i passaggi;
la segmentazione non è una selezione delle fonti né una riduzione dei token.
Gli ID sono validi insieme al catalogo della specifica esecuzione. Le preparazioni
web salvano sia `packet.json` sia `catalog.json`, così che gli intervalli non
vengano ricalcolati diversamente durante la generazione. Le preparazioni precedenti
senza catalogo lo costruiscono dalle proprie fonti al primo utilizzo.

## Fatti e rilievi documentali

L'estrazione deterministica supporta la rappresentazione testuale FHIR
degli artefatti storici e delle fixture. Riconosce
`Observation`, `MedicationRequest`, `MedicationAdministration` e `Procedure`.
Ogni fatto conserva tipo, etichetta, data presente nel record, stato, attributi
documentati e collegamento a fonte/evidenza. Nel pilota MIMIC i passaggi
delle fonti sono leggibili, mentre gli aggregatori di questo formato storico
non coprono i record tabellari MIMIC.

Per le osservazioni semplici vengono separati valore, eventuale comparatore e
unità. Pannelli, range e interpretazioni più complessi rimangono nel dettaglio
originale; non vengono trasformati in valori semplici. Il parser conserva i JSON
dei dosaggi senza attribuire significati a quantità prive di unità.

I controlli implementati segnalano:

- quantità di dose con valore ma senza `unit` o `code`;
- risultati semplici diversi con la stessa etichetta e la stessa data riportata.

Il secondo caso richiede lettura delle fonti: non risolve automaticamente differenze
fra campioni, versioni o correzioni. La data del record conserva il significato
dell'adattatore, per esempio `authoredOn` per una richiesta farmacologica;
non attesta da sola una somministrazione o una prescrizione finale.

L'inventario è **parziale**: non interpreta il testo libero, non identifica tutte
le discordanze cliniche e non dimostra che ogni frase della bozza sia sostenuta.
I rilievi con riferimenti sono inclusi nel prompt; gli attributi mancanti non
vengono completati automaticamente. L'inventario completo è salvato prima
dell'inferenza in `fatti_documentali.json`, anche in dry-run o in caso di errore.

## Tool disponibili

| Tool | Comportamento |
|---|---|
| `riepiloga_osservazioni(esame, dal, al, storico, limit, offset)` | Ultimi record per etichetta esatta, oppure serie completa con `storico=true`; conserva parità, valori, unità, stati e rilievi |
| `ricostruisci_terapia_documentata(farmaco, dal, al, limit, offset)` | Sequenze per etichetta esatta, con prescrizioni e somministrazioni distinte, dosaggi originali e campi strutturati mancanti |
| `ricostruisci_sequenza_eventi(tipo, dal, al, limit, offset)` | Eventi strutturati ordinati con attributi, stato e riferimenti |
| `leggi_evidenze(ids)` | Ispezione degli intervalli originali richiesti, con provenienza e hash |

Tutte le fonti restano nel prompt iniziale. Gli strumenti aggiungono elaborazioni
deterministiche: non recuperano nuovi dati dal DB. Ricerca lessicale e vecchia
cronologia di soli passaggi sono state sostituite. Ogni tool usa soltanto il
catalogo passato dall'applicazione: non accetta identificativi di altri pazienti,
URL, SQL o percorsi di file e non dispone di client DB o di rete.

Le osservazioni comprendono anche parametri vitali: il testo già estratto non
permette una classificazione affidabile del laboratorio. Non vengono convertite
unità né unificate etichette diverse. L'ultimo record è documentale, senza
selezione automatica per stato o giudizio di normalità. Se tutti gli orari hanno
un fuso si confrontano gli istanti UTC; altrimenti l'ordine è per sola data
riportata, conservando tutti i candidati dell'ultima data. Le parità non vengono
risolte arbitrariamente. I filtri temporali sono inclusivi sulle date riportate.

Le sequenze farmacologiche non sono una riconciliazione clinica. `active` e ultima
somministrazione non attestano terapia domiciliare o sospensione. I JSON dei
dosaggi restano originali; `missing_structured_fields` segnala dose, via o schema
temporale non esplicitati nei campi strutturati, anche se descritti nel testo
libero. L'assenza di unità viene segnalata dai rilievi documentali esistenti.

Ogni elaborazione espone `coverage`: fatti riconosciuti e passaggi non
interpretati. Copertura parziale e risultato vuoto non dimostrano assenza clinica.
Ogni evento conserva `source_id` ed `evidence_id`, verificati contro il catalogo.

Le risposte sono limitate per numero di elementi e byte UTF-8. `limit` (1–10)
conta gruppi per osservazioni/terapia, eventi per la cronologia. `offset` e
`next_offset` consentono pagine contigue; `matches_total` e `limited` dichiarano
quanto resta. Non si tagliano gruppi o passaggi. Un gruppo troppo grande richiede
filtri più stretti; la lettura esplicita fallisce se supera il budget. Per questa
sola lettura, i metadati completi di origine sono restituiti una volta per fonte
in `source_metadata`. Le elaborazioni fanno riferimento al catalogo senza
replicarne le origini estese.

### Separazione dei moduli

- `facts.py`: estrazione conservativa e contratti dei fatti documentali;
- `documentary.py`: selezione, raggruppamento e ordinamento, senza PydanticAI,
  telemetria, DB o trasporto;
- `tools.py`: confine PydanticAI, provenienza, paginazione, budget e audit;
- `observability/tool_spans.py` e `model_spans.py`: span TOOL, singole richieste
  LLM e riepiloghi AGENT, senza import di moduli del dominio;
- `generator.py`: ciclo del modello, streaming e budget complessivi.

L’autenticazione HTTP condivisa è in `http_auth.py`: l’API di generazione non
importa il backend SQL. L’extra `web` e la relativa immagine includono soltanto
FastAPI/Uvicorn oltre al nucleo applicativo; i driver PostgreSQL restano
nell’extra `backend`. Non sono state aggiunte librerie. I controlli di forma e dei
riferimenti sulla bozza restano automatici, indipendenti dalla scelta dei tool;
non attestano il supporto semantico di ogni frase clinica.

Il limite applicativo conta anche i tentativi rifiutati prima dell'esecuzione.
Un nome di tool o parametri non validi vengono restituiti al modello come feedback,
con una sola correzione consentita per tool (`tool_validation_retries=1`, oppure
`0` per arresto immediato). Le richieste aggiuntive consumano gli stessi budget
di richieste e token; non vengono ampliati automaticamente. L'output finale non
ha retry e il trasporto mantiene `max_retries=0`. Ogni operazione ha inoltre un
timeout di 10 secondi. Per i contratti del framework si vedano le documentazioni ufficiali
su [dipendenze](https://pydantic.dev/docs/ai/core-concepts/dependencies/),
[eventi e limiti](https://pydantic.dev/docs/ai/core-concepts/agent/) e
[retry](https://pydantic.dev/docs/ai/core-concepts/retries/).

L'esito distingue le letture eseguite (`tools.calls`) dai tentativi emessi dal
modello (`tools.attempts`, con nome autorizzato e `args_valid`). I nomi sconosciuti
sono sostituiti con un'etichetta tecnica; gli argomenti non vengono registrati.
I rifiuti di validazione producono span Phoenix `TOOL` con stato `rejected` e
indice del tentativo, senza input o messaggi di errore contenenti dati.
La UI distingue `tool_call_invalid`, `model_protocol_error` e `model_usage_limit`
dagli errori di inattività o durata totale. Il messaggio generico rimane solo
per errori non classificati.

La storia viene stimata nuovamente prima di ogni richiesta, con riserva per gli
schemi dei tool, output e margine; se eccede il budget, il lavoro si arresta.
Il massimo output viene ridotto quando necessario in base al budget aggregato
residuo stimato. La verifica dei token effettivi rimane legata all'usage fornito
dal serving, che può arrivare soltanto dopo una richiesta.
Se non c'è spazio per un'altra lettura seguita da una risposta finale, oppure
si arriva all'ultima richiesta consentita, l'applicazione aggiunge un messaggio
`# Fase finale obbligatoria` in coda. System, fonti, schemi dei tool, ID delle
chiamate e risultati originali restano invariati. La modalità è `final_append`,
con `prefix_strategy=append_only`: non viene ricostruita o ripulita la storia.

Il vincolo della fase finale è dichiarato nelle istruzioni statiche fin dalla
prima richiesta. Gli schemi restano disponibili per conservare il prefisso,
mentre l'esecuzione è bloccata da un controllo applicativo. Se il modello chiede
ancora un tool, `FinalPhaseToolCallError` arresta il run senza lettura né retry del
modello; la UI espone `tool_call_after_final`. Anche gli argomenti non validi
nella fase finale interrompono il ciclo, senza ulteriori correzioni.

Questo dettaglio conta con ds4.c: nel renderer pubblico gli schemi precedono il
system e `tool_choice=none` li esclude dal prompt. Perciò rimuoverli o cambiare
le istruzioni prima delle fonti può perdere l'intero prefisso. Il percorso
ordinario conserva gli schemi e non forza `none` durante la fase finale. Il
budget continua a includere schemi, storia completa, output e margine; viene
calcolato una volta per passo. Non vengono eliminate fonti o ampliati i limiti.
La scelta è registrata in `step_budgets[].final_only_reason`; il budget non viene
aumentato e non vengono eliminate fonti. Gli errori documentali attesi hanno
codici specifici (`unknown_evidence`, `result_budget`, ecc.) e possono essere
restituiti al modello come feedback entro la medesima correzione consentita.

## Stato, risultati e misure

L'API aggiunge `POST /api/v1/preparazioni/{id}/interrompi`; applica gli stessi
controlli di accesso delle altre operazioni. Gli stati includono `cancelling`.
Lo stato persistito contiene run ID, fase, avvio, aggiornamento e progressi
numerici. La seconda richiesta di arresto è idempotente. Un lavoro interrotto
rimane terminale: per riprovare occorre una nuova preparazione.

Il contratto di risultato è `1.1`, con `facts`; il lettore accetta anche i risultati
`1.0` senza inventario. Lo stato clinico resta `not_performed`. I riferimenti dei
fatti e dei rilievi devono appartenere al catalogo del risultato.

La risposta può essere JSON grezzo oppure un solo blocco Markdown `json`, senza
prosa esterna. L'involucro è gestito anche nell'anteprima, conservando i byte
originali negli artefatti. Non vengono corretti chiavi duplicate, sezioni o
riferimenti non validi. Una sezione priva di dati può usare la formula canonica
oppure elencare tutti i campi del template come `non disponibile`; un valore o
una negazione non rientrano in questa regola e richiedono le citazioni.

Con i tool, eventuali preamboli del modello prima di una lettura non entrano
nella lettera. Il file parziale segue il testo dell'ultima risposta del modello,
aggiornato durante lo stream; il JSON finale è pubblicato dopo i controlli
esistenti. In caso di errore può essere incompleto o contenere un preambolo e
non viene presentato come lettera completata.

L'usage è conservato per richiesta e aggregato, senza sovrascriverlo con quello
dell'ultima chiamata. Se manca un contatore, l'aggregato resta sconosciuto.
Le misure aggregate del ciclo includono anche l'intervallo dei tool; i dati
per richiesta conservano il confine del provider. Il registro locale dell'esito
include operazioni dei tool, stato e ID restituiti, senza query o testi delle
evidenze. Prompt, parziali, inventario e fonti restano negli artefatti locali;
non vengono aggiunti alle tracce esportate.

## Verifica e diagnosi

```bash
PYTHONPATH=src uv run --locked --extra backend pytest tests/discharge tests/web_api -q
uv run --locked ruff check src tests
```

I test simulano errori e correzioni dei tool, metadati estesi, limiti di budget,
streaming e salvataggio. Non sostituiscono il collaudo del serving reale.

| Codice pubblico | Significato |
|---|---|
| `model_inactivity_timeout` | Nessun progresso prima del testo entro l'intervallo configurato |
| `total_timeout` | Raggiunto un limite totale richiesto esplicitamente |
| `tool_call_invalid` | Nome o parametri del tool rifiutati, senza ulteriori correzioni disponibili |
| `tool_call_after_final` | Tool richiesto dopo la chiusura delle letture; arresto senza nuova inferenza |
| `tool_read_*` | Lettura fuori catalogo, oltre il budget o altro errore documentale atteso |
| `model_protocol_error` | Risposta del modello non utilizzabile |
| `model_usage_limit` | Esaurito un budget di richieste, tool, contesto o token |

Dopo un'interruzione il rilascio effettivo delle risorse sullo Spark va verificato
sul serving; la chiusura client non lo attesta.

Dopo modifiche al codice ricostruire i servizi prima di verificarli:

```bash
docker compose build agent-api web
docker compose up -d --no-deps --wait agent-api web
```

Prima del riavvio verificare che non ci siano generazioni attive. Gli artefatti
originali, compresi i tentativi falliti, restano conservati.


Per la gerarchia AGENT/LLM/TOOL, i campi per singola richiesta e i totali del ciclo,
vedere [osservabilità](../observability/README.md#phoenix-che-cosa-guardare).
