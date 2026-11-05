# Confronto fra MIMIC-IV-Note e CAMPUS

Il modello CAMPUS 1.1.0 resta il formato della bozza e dell'interfaccia di
revisione. La lettera MIMIC-IV-Note è il riferimento narrativo dello stesso
ricovero. Il confronto usa i contenuti clinici per sezione, anche quando sono
distribuiti diversamente fra i due documenti. Le intestazioni orientano la
revisione. La corrispondenza delle parole è poco informativa perché la bozza è in
italiano e il riferimento in inglese.

## Inventario e struttura dei file locali

I due download occupano **6,05 GiB compressi**. Hosp è l'estratto relazionale
dell'EHR ospedaliero, con **22 CSV gzip, 299.712 pazienti e 431.231 ricoveri**.
Note aggiunge quattro CSV gzip con documenti testuali e metadati. I conteggi
Hosp coincidono con quelli del rilascio 2.2. Le lettere contengono un campo
`text` multilinea delimitato da virgolette CSV, per cui una riga fisica del
file può essere soltanto una parte della nota. [MIMIC-IV 2.2](https://physionet.org/content/mimiciv/2.2/)
· [MIMIC-IV-Note 2.2](https://physionet.org/content/mimic-iv-note/2.2/).

| Famiglia | Tabelle e forma |
|---|---|
| Pazienti e ricoveri | `patients` ha una riga per persona. `admissions` ha una riga per ricovero con orari, tipo, provenienza e destinazione. `transfers` e `services` rappresentano spostamenti e cambi di servizio. `provider` contiene identificativi pseudonimi. |
| Misure e microbiologia | `labevents` contiene misurazioni con `itemid`, valore testuale o numerico, unità e orari. `d_labitems` descrive gli esami. `microbiologyevents` registra campioni, colture, organismi e sensibilità. `omr` usa coppie nome/valore datate a livello di paziente. |
| Ordini e farmaci | `poe` e `poe_detail` registrano ordini e attributi. `prescriptions` e `pharmacy` descrivono prescrizioni e gestione farmaceutica. `emar` ed `emar_detail` registrano eventi di somministrazione con uno stato, che può anche indicare una dose non data. |
| Codifica amministrativa | `diagnoses_icd` e `procedures_icd` associano codici ai ricoveri. I rispettivi dizionari si collegano su codice e versione. `hcpcsevents`, `d_hcpcs` e `drgcodes` aggiungono codici di prestazioni e raggruppamenti amministrativi. |
| Lettere e referti | `discharge` e `radiology` hanno una riga per nota, con `note_id`, chiavi di paziente e ricovero, orari e testo. `discharge_detail` e `radiology_detail` aggiungono metadati come coppie nome/valore per `note_id`. |

Il join locale di `discharge` con `admissions` sulla coppia
`(subject_id, hadm_id)` trova **331.793 lettere, una per ricovero**, per
145.914 pazienti. Copre il **76,94%** delle ammissioni presenti. Il CSV
corrisponde al checksum distribuito. L'abstract di PhysioNet riporta
331.794 lettere e 145.915 pazienti. I conteggi qui si riferiscono ai file
locali verificati. I checksum coincidono per tutti e quattro i file Note.

Dei **2.321.355 referti radiologici** locali, **1.176.597** hanno
`hadm_id` vuoto. I restanti **1.144.758** si collegano a un ricovero Hosp
tramite entrambe le chiavi. Di questi, **817.440** rispettano il vincolo
temporale `admittime <= charttime <= storetime <= dischtime`, prima dei
limiti di lunghezza, numero di referti e selezione dei ricoveri. Il pilota
conserva il legame esplicito con il ricovero.

MIMIC-IV proviene da cure reali al Beth Israel Deaconess Medical Center.
La deidentificazione trasla le date con un offset per paziente. Gli
intervalli temporali dello stesso paziente restano interpretabili, mentre le
date di pazienti diversi non formano una cronologia di calendario comune.
I file locali comprendono Hosp, Note, ED e ICU. Le immagini radiologiche
appartengono a un modulo distinto.
[Schema MIMIC](https://mimic.mit.edu/docs/iv/about/schema-overview.html).

L'estratto misto `hosp-note-ed-icu-2.2-v1` estende le fonti del pilota Hosp.
L'OMR ha la chiave del paziente ma manca del legame esplicito col ricovero.
La tabella `provider` contiene pseudonimi. Queste due tabelle rimangono sul
disco fuori dal pacchetto. Il [catalogo dei dataset](dataset-mimic.md) raccoglie
le estensioni da scaricare e il comando ICU.

## Abbinamento del pilota

- Fonti locali compatibili con [MIMIC-IV 2.2](https://physionet.org/content/mimiciv/2.2/)
  e [MIMIC-IV-Note 2.2](https://physionet.org/content/mimic-iv-note/2.2/).
  Il conteggio locale di 431.231 ammissioni è coerente con Hosp 2.2.
- Unità di analisi. Ricovero concluso con lettera non vuota, abbinato per
  `subject_id` e `hadm_id`. Il campione misto seleziona 20 pazienti distinti
  con seed 42, cinque per ciascuno dei quattro percorsi. Hosp con ED e ICU,
  Hosp con ED, Hosp con ICU e Hosp senza una permanenza ED/ICU collegabile
  secondo i filtri adottati. La selezione alterna i gruppi e privilegia servizi
  e categorie della prima diagnosi ICD meno rappresentati nel campione.
- ED si collega al ricovero con `edstays` e alle sue tabelle con
  `subject_id` e `stay_id`. Il triage può precedere l'ingresso in Hosp.
  ICU usa `icustays`, poi verifica `subject_id`, `stay_id` e `hadm_id`
  sulle righe cliniche. Le misure e gli interventi sono limitati alla
  permanenza. Per ICU, la registrazione deve essere disponibile entro
  la dimissione ospedaliera. L'estrazione conserva un numero limitato di
  eventi per fonte, con prima e ultima osservazione per le serie ICU.
- Si conservano tutte le lettere abbinate. Per un confronto riproducibile
  si usa l'ultima per `storetime`, poi `charttime` e `note_id`. Questo
  criterio di ordinamento non certifica quale documento fosse stato firmato.
- Hosp fornisce dati amministrativi, trasferimenti e servizi, codici ICD,
  HCPCS e DRG, laboratori, ordini di reparto, farmacia, eMAR con dettagli,
  microbiologia e ordini POE con attributi. I referti provengono da
  `radiology`, con legame esplicito al ricovero e disponibilità entro l'uscita.
- Per ricovero si selezionano fino a 60 item di laboratorio, con prima misura
  e ultime due, fino a 40 ordini in degenza distinti per farmaco e via,
  64 eventi eMAR distinti per farmaco e stato, 48 righe di microbiologia,
  40 ordini POE distinti per categoria e stato, e 6 referti fino a 10.000
  caratteri ciascuno. Per eMAR, laboratorio e radiologia contano il tempo
  dell'evento e quello di registrazione. In microbiologia, se sono presenti
  solo le date, si accettano i giorni interni alla degenza.
- La fonte separata della terapia finale ammette fino a 40 ordini espliciti
  `Discharge Med` attivi, verificati entro la dimissione e collegati a
  `prescriptions` per ricovero e `pharmacy_id`. Lo stato eMAR resta
  distinto dall'ordine. POE attesta ordini e stati, compreso l'annullamento.
- Nel precedente campione Hosp, i 20 ricoveri avevano 218 fonti. Gli ordini
  POE erano disponibili in 20 ricoveri, eMAR e microbiologia in 8 ciascuno,
  HCPCS in 4 e DRG in 16. Nessun ordine `Discharge Med` soddisfaceva tutti
  i criteri. Le richieste avevano 11.383–32.055 token stimati di input.
- Il campione misto importato ha 363 fonti, 16 servizi iniziali e 20
  categorie ICD in posizione 1. Dieci ricoveri hanno una fonte di triage
  e dieci hanno misure ICU. Nessun ordine `Discharge Med` soddisfa tutti
  i criteri. Le 20 richieste sono state validate localmente, con
  11.881–46.863 token stimati di input. L'indice privato per la prova
  in CAMPUS è in `$TESI_PRIVATE_DIR/var/mimic/casi-test.md`.
- Per il pilota usare la generazione ordinaria. Le fonti testuali MIMIC sono
  leggibili per passaggio. Gli aggregatori agentici dei fatti strutturati
  sono stati sviluppati sul formato dei record Synthea.
- I codici amministrativi ICD sono retrospettivi. Questo pilota valuta
  la ricostruzione documentale a posteriori. Per una prova che simuli la
  stesura prima della dimissione, il protocollo deve definire un istante
  di taglio e una variante senza codifica post-hoc.
- Le lettere originali restano nella tabella `mimic_discharge_references`.
  CAMPUS mostra l'ultima lettera abbinata quando l'utente apre **Lettera di
  dimissione originale** in fondo alla degenza. Un servizio di revisione in
  sola lettura controlla assegnazione e versione del ricovero. Il servizio
  di compilazione può leggere solo contesti e fonti di compilazione.
  L'inferenza usa il serving privato sul DGX Spark o un
  endpoint loopback. L'export della telemetria è disattivato per il flusso MIMIC.
  Originali, bozze e confronti sono dati ad accesso ristretto secondo le
  [indicazioni PhysioNet](https://physionet.org/news/post/llm-responsible-use/).

## Corrispondenza dei 17 campi

Nel pilota locale di 20 ricoveri con una lettera ciascuno, le intestazioni
`Discharge Medications`, `Discharge Condition` e `Discharge Disposition`
compaiono in 20 lettere, `Discharge Diagnosis`, `Discharge Instructions` e
`Followup Instructions` in 19, `Pertinent Results` in 18 e `Brief Hospital
Course` in 17. `History of Present Illness` e `Past Medical History` compaiono
in 20. Nessuna intestazione riconosciuta per esami in attesa compare nel
campione. Il conteggio misura le intestazioni riconosciute e lascia aperta la
presenza di informazioni corrispondenti nel testo narrativo.

| Campo CAMPUS | Dove cercare nel riferimento MIMIC | Criterio di revisione |
|---|---|---|
| Identificativi e struttura | Intestazione, Date of Birth, Service | Chiavi dal join e servizio documentato. Anagrafica oscurata e date traslate. |
| Data di dimissione | Discharge Date | Data effettiva di `admissions`; confrontabile con la data riportata nella nota. |
| Diagnosi testuali | Discharge Diagnosis, Brief Hospital Course | Confrontare le diagnosi esplicitamente finali, preservando sospetti e negazioni. |
| Diagnosi codificate | Associazione a `diagnoses_icd` | Verificare codice e versione nella fonte strutturata. Il testo della lettera può omettere i codici. |
| Procedure codificate | Associazione a `procedures_icd` | Verificare codice e versione nella fonte strutturata, separatamente dal nome della procedura. |
| Motivo del ricovero | Chief Complaint, History of Present Illness | Il tipo amministrativo di ingresso copre un'informazione diversa dal motivo clinico. |
| Problemi non attivi | Past Medical History | Separare problemi risolti o pregressi espliciti da comorbilità ancora attive. |
| Procedure diagnostiche e terapeutiche | Major Surgical or Invasive Procedure, Pertinent Results, Brief Hospital Course | Distinguere procedura effettuata, richiesta, esito e cronologia. |
| Esami di laboratorio | Pertinent Results | Confrontare valore, unità e momento del prelievo, tenendo conto della selezione dei risultati. |
| Epicrisi e decorso | Brief Hospital Course | Confrontare eventi chiave, sequenza, incertezza e risposta ai trattamenti se documentata. |
| Condizione alla dimissione | Discharge Condition, Discharge Disposition | Valutare separatamente stato clinico, autonomia e destinazione. |
| Programma terapeutico e follow-up | Discharge Instructions, Followup Instructions | Confrontare indicazione, destinatario e tempistica quando le fonti le supportano. |
| Terapia alla dimissione | Discharge Medications | Confrontare farmaco, dose, via, frequenza e durata quando espliciti. Gli ordini di reparto hanno finalità diversa. |
| Contatti e urgenze | Discharge Instructions, Followup Instructions | Corrispondenza parziale. I recapiti possono essere oscurati o legati a un altro sistema sanitario. |
| In attesa di istologico | Pending Results o testo narrativo | Un esame in attesa è pertinente se identificato esplicitamente come istologico. L'assenza di menzione lascia il campo da completare. |
| Informazioni sull'istologico | Pending Results, Brief Hospital Course, Discharge Instructions | Valutare dettagli e modalità di ritiro solo con attesa istologica documentata. |
| Medici | Attending o firma, se presente | Ruoli potenzialmente diversi e nominativi spesso oscurati. |

## Guida di stesura MIMIC

Per il dataset MIMIC, il prompt `campus-mimic-v4` riporta l'origine di ogni
fonte e usa la guida MIMIC con le stesse 17 sezioni CAMPUS. I codici ICD
retrospettivi vanno nei campi codificati con l'indicazione «codifica
amministrativa del ricovero». L'epicrisi può essere breve quanto consentono
le evidenze disponibili. La versione del prompt viene registrata in `esito.json`.

I test locali verificano il prompt e il formato delle risposte attese su
destinazione amministrativa isolata, ordine di reparto, codice diagnostico,
codice procedurale e biopsia senza esito. Le risposte di prova sono scritte
come fixture e validate localmente. La loro validazione riguarda struttura e
citazioni. L'aderenza del modello alle istruzioni richiede una prova di inferenza.

## Revisione locale

```bash
docker compose run --rm backend-admin profile-mimic
docker compose run --rm --user "$(id -u):$(id -g)" \
  -v "${TESI_PRIVATE_DIR:-$HOME/Desktop/codice-tesi-private}/output/dimissioni-mimic:/confronti" \
  backend-admin compare-mimic \
  /confronti/<ricovero>/<esecuzione>/bozza_strutturata.json \
  /confronti/confronto-pilota.md
```

Sostituire i due segnaposto con la cartella restituita dalla generazione.
`compare-mimic` verifica ricovero, impronta del campione e template, poi salva
in un file privato la tabella di corrispondenza, la bozza e il riferimento.
`profile-mimic` pubblica nel terminale soltanto i conteggi di intestazioni note.
È una scansione strutturale del campione, con possibili varianti di
intestazione e informazioni distribuite nel testo libero.

Per la revisione semantica registrare per ogni campo il supporto nelle fonti
ammesse e il riscontro nel riferimento. Separare fatti supportati e concordanti,
omissioni a fronte di evidenza disponibile, contraddizioni, aggiunte prive
di evidenza e contenuti presenti solo nel riferimento. Il tasso di compilazione
CAMPUS misura la disponibilità di campi nel formato locale. La valutazione
clinica riguarda separatamente fedeltà, omissioni e informazioni che il modello
ha introdotto.
