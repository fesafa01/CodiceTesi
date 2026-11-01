# Tracciabilità della bozza: contratto 1.0

Stato: prima implementazione del 1 novembre 2026, a partire dalla specifica del
28 ottobre. La generazione richiede il JSON delle 17 sezioni con citazioni e
produce le viste dopo i controlli tecnici. Una evidenza copre inizialmente una
fonte completa: la localizzazione è grossolana. Non è una verifica semantica.
L'esempio JSON allegato resta didattico, senza dati di pazienti, e non proviene
da Synthea. Il suo stato `not_run` non rappresenta una esecuzione del prototipo.

## Scelta di base

La prima versione collega ogni sezione a una o più fonti e alle evidenze
selezionate al loro interno. Il medico può leggere queste evidenze accanto alla
sezione. Non viene ancora associata ogni singola frase della bozza a una fonte.

Una **fonte** è un oggetto informativo ricevuto dal backend, identificato e
versionato: un record strutturato, una nota oppure un documento. Una
**evidenza** è un passaggio indirizzabile della rappresentazione di quella fonte
effettivamente fornita al modello. Una **citazione** è il collegamento proposto
dal modello fra una sezione e una o più evidenze del catalogo.

Il backend fornisce le fonti; l'applicazione prepara il catalogo delle evidenze
prima dell'inferenza. Il modello sceglie
soltanto riferimenti già esistenti. Non genera origine, hash, estratti, offset o
nuovi identificativi di evidenza. Le citazioni sono proposte di supporto
documentale, non una prova del processo interno del modello.

## 1. Catalogo preparato dall'applicazione

Il catalogo riusa gli attributi di `EvidenceSource`: `id`, `context_id`, `kind`,
`text`, `origin`, `version`, `sha256`. `kind` resta descrittivo: nessuna categoria
specifica di un dataset determina quali sezioni si possano compilare.

Il nuovo elenco `evidences` contiene:

| Campo | Tipo e significato |
|---|---|
| `id` | Stringa non vuota, univoca nel catalogo della richiesta |
| `source_id` | ID di una fonte presente nello stesso catalogo |
| `start` | Intero, primo carattere incluso in `source.text` |
| `end` | Intero, primo carattere escluso in `source.text` |

L'estratto si ottiene con `source.text[start:end]`. Gli indici contano punti di
codice Unicode, come lo slicing Python; non byte UTF-8, token o unità UTF-16.
Valgono `0 <= start < end <= len(source.text)` e testo non vuoto dopo `strip()`.
Non si applicano normalizzazioni dopo aver assegnato offset e hash. L'hash della
fonte è SHA-256 del suo testo codificato UTF-8, come nel modello attuale.

Il prompt mostra ID della fonte, ID delle evidenze e contenuti relativi. Il
catalogo completo rimane salvato localmente. La segmentazione deve preservare
contesto, negazioni e riferimenti temporali; non deve introdurre riassunti o
interpretazioni cliniche. Le evidenze possono sovrapporsi. Per la prima prova è
ammessa una sola evidenza che copre tutta una fonte, dichiarando che la
localizzazione è grossolana.

### Indipendenza dal dataset

L'adattatore del dataset produce una rappresentazione testuale fedele e conserva
in `origin` un riferimento recuperabile all'originale. Per un record JSON il
testo può conservare nomi dei campi, valori, date e unità presenti; per una nota
può coincidere con il testo originale. Gli ID interni del catalogo sono opachi:
il modello non deve conoscere nomi di file, tabelle o sistemi clinici.

Gli offset identificano sempre il testo fornito al modello. Non rappresentano
automaticamente una pagina del PDF o un percorso del JSON originale. Quando
arriverà il nuovo dataset, l'adattatore dovrà conservare anche la corrispondenza
fra rappresentazione e originale, con selettori adatti a quel formato. Il
contratto delle citazioni nelle sezioni potrà rimanere identico.

Anche i valori del contesto verificato utilizzati nella lettera, per esempio gli
identificativi, sono rappresentati nel catalogo da una fonte tecnica preparata
dall'applicazione a partire da `CompositionPacket.context`. L'origine dichiarata
è `CompositionPacket.context`, distinta dalle fonti cliniche del dataset. L'ID è
assegnato senza collisioni con le fonti ricevute. Le citazioni non autorizzano
il modello a modificare gli identificativi.
L'indipendenza dal dataset non permette di inventare identità o legami clinici
che il nuovo materiale non contiene.

## 2. Risposta richiesta al modello

La risposta contiene solo `sections`. Ecco un frammento, non una lettera completa:

```json
{
  "sections": [
    {
      "section_id": "esami_laboratorio",
      "text": "Sodio: 139 mmol/L (12/10/2026).",
      "sources": [
        {"source_id": "fonte-001", "evidence_ids": ["evidenza-001"]}
      ]
    }
  ]
}
```

| Campo | Regola |
|---|---|
| `section_id` | ID canonico del template, non il titolo libero |
| `text` | Contenuto della sezione, senza numero e intestazione |
| `sources` | Elenco dei riferimenti proposti per quella sezione |
| `sources[].source_id` | Fonte già presente nel catalogo |
| `sources[].evidence_ids` | Elenco non vuoto di evidenze appartenenti a quella fonte |

La risposta completa contiene tutte le sezioni del template una sola volta e
nell'ordine canonico, anche se vuote. Non sono ammesse proprietà aggiuntive,
fonti duplicate nella stessa sezione o evidenze duplicate nello stesso gruppo.
La stessa fonte o evidenza può supportare sezioni diverse. Le intestazioni e il
titolo della lettera vengono aggiunti dal renderer del codice.

Non servono punteggi di confidenza né un'etichetta automatica «clinicamente
validato». Se la sezione ha dati documentati solo in parte, il testo conserva i
dati disponibili e segnala gli attributi mancanti, citando le evidenze presenti.

### Lacune, assenze e discordanze

- Se mancano completamente le informazioni, si mantengono le regole attuali del
  template: formula di indisponibilità o testo vuoto. `sources` è `[]`; non si
  inventa una fonte che provi l'assenza di informazioni.
- Un'assenza esplicitamente documentata, come una risposta negativa prevista dal
  template, richiede invece l'evidenza che la dichiara. La mancata registrazione
  non equivale a una negazione.
- Se le fonti sono in contrasto, la sezione segnala la discordanza e cita le
  evidenze pertinenti di entrambe, senza scegliere arbitrariamente.
- Una sezione non applicabile segue le condizioni del template. Il solo elenco
  vuoto di fonti non significa «non applicabile» né «nessun problema clinico».

## 3. Controlli e loro significato

Il codice verifica schema, proprietà e chiavi JSON duplicate, ID e ordine delle
sezioni, appartenenza di fonti ed
evidenze al contesto, integrità del catalogo, offset e corrispondenza fra fonte
ed evidenze citate. Una sezione con contenuto fattuale deve avere almeno un
riferimento; fanno eccezione i testi esatti di indisponibilità e i casi vuoti
previsti dal template. Le sezioni obbligatorie o con obbligatorietà da confermare
non possono essere vuote. Una sezione vuota o con la sola formula di
indisponibilità deve avere `sources: []`. Si applicano anche le regole attuali
del binario istologico e dei suoi dettagli.

Le verifiche tecniche non dimostrano pertinenza, completezza delle citazioni o
correttezza clinica del testo. Con collegamenti per sezione una sola evidenza
non garantisce supporto a tutte le frasi: questo limite va dichiarato nella
valutazione. Negazioni, tempi, valori, stato dei farmaci e conflitti richiedono
verifica sul contenuto; la revisione del medico resta necessaria.

Un riferimento sconosciuto o appartenente a un'altra fonte produce un errore
tecnico. Non viene eliminato silenziosamente. Si conserva la risposta grezza
come parziale e non si pubblica la bozza come tecnicamente verificata. Non si
aggiungono retry o chiamate LLM automatiche.

## 4. Artefatti previsti

Si mantiene una richiesta al modello per l'intera lettera. L'applicazione
costruisce un risultato locale con questi blocchi, illustrati in
`tracciabilita.esempio.json`:

- `schema_version`: versione del contratto di tracciabilità, separata dalle
  versioni di `CompositionPacket` e del template;
- `binding`: `run_id`, `context_id`, identificativo e versione del dataset,
  identificativo, versione e hash del template, copiati dai metadati verificati;
- `catalog`: fonti ed evidenze preparate dall'applicazione;
- `draft`: la risposta del modello con `sections`;
- `validation`: stato tecnico `not_run`, `passed` o `failed`, e stato della
  verifica clinica. In questa prima versione `clinical_review` è sempre
  `not_performed`: non esiste ancora un flusso di revisione registrata.

`binding`, `catalog` e `validation` non sono compilati dal modello. Gli ID di
evidenza sono validi solo insieme a questo catalogo e a queste versioni.

Nella cartella della singola esecuzione vengono salvati:

| Artefatto | Contenuto |
|---|---|
| `catalogo_evidenze.json` | Catalogo completo, salvato prima dell'inferenza anche in dry-run e nei tentativi falliti |
| `risposta.parziale.json` | Risposta LLM in streaming, anche incompleta o invalida |
| `bozza_strutturata.json` | Risultato con binding, catalogo, sezioni e controlli, dopo il completamento tecnico |
| `bozza.txt` | Lettera renderizzata da `draft.sections` con titoli canonici |
| `evidenze.md` | Vista per sezione con fonte, origine ed estratti recuperati dal catalogo |
| `esito.json` | Esito tecnico e hash degli artefatti, senza duplicare i contenuti clinici |

`risposta.parziale.json` conserva il nome anche quando lo stream termina con
successo: contiene la risposta grezza esatta e non viene trasformato o eliminato.
In caso di errore nei riferimenti, JSON invalido o interruzione, non si produce
`bozza.txt` come risultato completato. In dry-run vengono salvati soltanto
richiesta, catalogo ed esito, senza avviare il client LLM.

`richiesta.txt` e il registro attuali restano parte degli artefatti. I contenuti
del catalogo e delle evidenze seguono la conservazione e il perimetro locale
degli altri contenuti clinici, senza essere aggiunti alla telemetria.

I due documenti leggibili derivano dallo stesso risultato strutturato. Se il
medico modifica `bozza.txt`, il suo hash non corrisponde più a quello registrato:
le citazioni precedenti restano riferite alla bozza generata e non certificano
la riscrittura. Una futura interfaccia di revisione dovrà conservare versioni e
richiedere la riconferma dei collegamenti delle sezioni modificate.

## 5. Integrazione nel prototipo

`discharge/provenance.py` definisce catalogo, citazioni, sezioni e risultato, con
controlli sui riferimenti e renderer delle viste. `prepare_request` costruisce
il catalogo una sola volta, lo include nel prompt e ne considera il testo nel
budget, senza troncamenti. Il prompt è `campus-stesura-v14-citazioni`.

`LLMDraftGenerator` mantiene una richiesta, zero tool e zero retry. La risposta
JSON viene analizzata soltanto dopo un completamento normale. `RunArtifacts`
verifica i riferimenti, renderizza la lettera e applica `validate_draft` prima di
salvare le viste. Il risultato e l'esito sono legati al run ID del registro;
versioni del dataset e template e hash degli artefatti restano recuperabili.
Il catalogo e la bozza non vengono aggiunti alla telemetria. Il contratto HTTP
del pacchetto e il dataset rimangono quelli già usati dal prototipo.

Il completamento tecnico produce una bozza da revisionare. Non sono introdotti
un secondo agente, correzioni automatiche o giudizi clinici sulle citazioni.

## Esempio allegato

Il file allegato contiene tutte le 17 sezioni del template corrente. Solo
`esami_laboratorio` è compilata con un dato fittizio; le altre rispettano le
regole di indisponibilità e i campi facoltativi/condizionali vuoti. Gli ID del
dataset sono esemplificativi, gli offset e gli hash sono calcolati sui testi
allegati. Lo stato `not_run` descrive il futuro controllo applicativo, che non è
ancora implementato; l'esempio può essere controllato separatamente.
