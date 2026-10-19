# Dati eCREAM

Il backend importa esplicitamente `notes.jsonl` eCREAM. Ogni riga non vuota
è un oggetto JSON con `note_id` intero e `text` non vuoto. `split` identifica
la provenienza; se assente viene registrato come `unknown`.

Il testo deve contenere un solo marcatore riconosciuto:

| Marcatore | Tipo della fonte |
| --- | --- |
| `---TRIAGE---` | `triage` |
| `---ANAMNESIS---` | `anamnesi` |
| `---MEDICAL_VISIT---` | `visita` |
| `---CLINICAL_DIARY---` | `diario` |
| `---SPECIALIST_CONSULTANCY---` | `consulenza` |
| `---NURSING_CARE_NOTES---` | `assistenza` |
| `---DISCHARGE---` | `dimissione` |

## Raggruppamento e provenienza

`importer.py` raggruppa le righe per `note_id`. Ogni gruppo diventa un contesto
`ecream-note-<note_id>`; ogni testo distinto diventa una fonte. Le righe con lo
stesso testo nello stesso gruppo vengono deduplicate, unendo le provenienze.
ID diversi restano separati anche quando i testi coincidono.

Un contesto con un solo testo ha ambito `nota_isolata`; con più testi ha ambito
`gruppo_note`. Il raggruppamento non certifica l'identità di un paziente o di un
episodio: `patient_id` ed `episode_id` restano null. Le annotazioni CRF non
vengono usate come fatti clinici o risposte attese.

Ogni fonte conserva testo originale, tipo, provenienza, versione e SHA-256.
La versione del dataset è il digest dell'intero file JSONL: anche una modifica
alla sola formattazione cambia la versione. Le fonti hanno identificativi
stabili derivati dal `note_id` e, nei gruppi con più testi, dal digest del testo.

## Persistenza

`composition_contexts` conserva ID e payload del contesto;
`composition_sources` conserva ID, riferimento al contesto e payload della fonte.
Chiavi esterne e vincoli verificano il legame fra gli identificativi SQL e JSON.

Tutti i contesti e le fonti vengono salvati in una sola transazione. Un lock
serializza le importazioni concorrenti. Ripetere l'importazione della stessa
versione non duplica i dati; una versione diversa di un dataset già presente
viene rifiutata e richiede un database separato.

Il pacchetto HTTP include tutte le fonti del contesto ordinate per ID insieme
al template Campus incluso nell'applicazione. Il tipo di nota non filtra le
fonti e non seleziona sezioni della lettera.

Per avvio e comandi vedere il [manuale del backend](backend.md).
