# Contratto dei dati e importazione

La repository non contiene più il dataset clinico generato. Il servizio si
inizializza senza dati: non ricrea pazienti, documenti, template o scenari.

## Loader locale conservato

`dataset.py` valida un formato proprietario JSON/PDF; `importer.py` lo importa
in una transazione PostgreSQL. Sono strumenti riutilizzabili, non un dataset
e non adattatori HL7. L'importazione richiede `--dataset-dir` o
`CLINICAL_DATASET_DIR` e uno schema già creato con `init`.

Il manifesto locale richiede `schema_version: "2.0"`, `versione`,
`sintetico: true`, `snapshot` con fuso orario, `scenario_iniziale`, `descrizione`
e le voci `files`: reparti, pazienti, ricoveri, prestazioni, modello_lettera,
fonti, scenari e copertura. Lo scenario iniziale deve essere fra quelli dichiarati.
Non vengono assunti identificativi, date o numeri di pazienti predefiniti.

Le registrazioni sono JSON con identità, contesto, autore, date, sezioni ed
misure eventuali. I documenti hanno metadati JSON, un originale PDF e un'eventuale
estrazione testuale JSON separata, con hash e versione della fonte. Le quantità
sono strutturate nelle sorgenti: il backend non le estrae dal PDF.

Il loader controlla percorsi relativi, estensioni, JSON UTF-8 senza chiavi
ripetute o NaN/Infinity, hash e dimensioni, leggibilità dei PDF, pagine delle
estrazioni, contesto paziente/episodio e cronologia delle versioni.
Il contratto locale accetta soltanto dati dichiarati sintetici. L'importazione
FHIR/CDA e la gestione di dataset reali richiedono adattatori e un contratto di
provenienza dedicati.

## Database e rappresentazioni

Originali, sezioni, misure e metadati sono conservati nel database.
Le API non leggono file del dataset durante le richieste. Gli elenchi su una
base vuota sono vuoti e dichiarano `dataset_caricato=false`.

L'adattatore esistente espone FHIR **R4B 4.3.0** in JSON, con negoziazione
`Accept` e `_format`. Patient, Encounter, Observation, DiagnosticReport e
DocumentReference sono proiezioni SQL. I Bundle `searchset` sono risultati di
ricerca, non una cartella trasferibile come documento FHIR completo.

Non sono implementati importazione HL7 v2/CDA/FHIR, PDF/A, firma digitale,
OCR o validazione completa rispetto alle IG italiane. La libreria
`fhir.resources` valida modelli Python, non converte file arbitrari.
Per i comandi vedere il [manuale](backend.md).

## Fixture dei test

I test costruiscono piccoli file in `tmp_path` e schemi temporanei nel database
di test. Usano identificativi e contenuti tecnici per verificare isolamento,
versioni, integrità e contratti HTTP, senza distribuire cartelle cliniche o
alimentare il servizio di sviluppo. Non costituiscono il nuovo benchmark clinico.
