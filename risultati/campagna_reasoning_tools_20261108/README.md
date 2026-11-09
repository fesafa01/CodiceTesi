# Campagna reasoning / reasoning con tool — chiusura 8 novembre 2026

180 tentativi sugli stessi 30 casi (15 Hosp, 15 Hosp + ED), tre modelli e due
condizioni. Tutti gli esiti terminali sono conservati, senza selezione dei
tentativi riusciti. I controlli tecnici non costituiscono una valutazione clinica.

| Modello | Condizione | Completed | Failed | Validazione tecnica superata | Prove con chiamate tool |
|---|---|---:|---:|---:|---:|
| Qwen | Reasoning | 30 | 0 | 26 | 0 |
| Qwen | Reasoning + tool disponibili | 30 | 0 | 28 | 25 |
| Gemma | Reasoning | 30 | 0 | 27 | 0 |
| Gemma | Reasoning + tool disponibili | 30 | 0 | 30 | 0 |
| Mistral | Reasoning | 30 | 0 | 19 | 0 |
| Mistral | Reasoning + tool disponibili | 19 | 11 | 12 | 0 |

Gli esiti di validazione riportati sono finali, dopo le eventuali riparazioni
deterministiche già previste dal sistema. L'uso effettivo dei tool è distinto
dalla loro abilitazione. Qwen ha effettuato 76 chiamate in 25 prove; Gemma e
Mistral nessuna. Il riepilogo completo è in `riepilogo.json`.

## Collocazione e conservazione

- Codice e documentazione: repository Git `CodiceTesi`.
- Originali privati: le tre cartelle `TEST-SPARK-<MODELLO>-TOOLS-v1`, i tre
  checkpoint `campagna-<modello>-reasoning-tools-v1.json` e i log disponibili
  nella directory sorella `codice-tesi-private`.
- Copia verificata: `codice-tesi-private/campagne/20261107-08-reasoning-tools-v1`.
  Contiene archivi, input preparati, checkpoint, log disponibili, sorgenti alla
  chiusura, stato Git e manifest SHA-256 per 1.605 file copiati.
- Le Base precedenti e le prove superate restano nei percorsi privati già
  documentati. Non sono state eliminate o mescolate a questa campagna.
- Nuovi esperimenti: directory privata `esperimenti`, con piani, output e log
  propri; non devono scrivere negli archivi di questa campagna.

La copia dei sorgenti rappresenta il workspace alla chiusura, con modifiche
locali. Gli identificativi software registrati nei singoli run restano il
riferimento per il codice effettivamente eseguito; il solo HEAD Git non basta.
La copia verificata è locale e non sostituisce un backup su un altro supporto.

## Analisi successiva

Le metriche usuali di token, tempi e hardware sono conservate insieme ai
contatori dei tool. Gli script dei vecchi grafici vanno adattati alle nuove
condizioni; i vecchi confronti Base/tool non rappresentano questa campagna.
La differenza di effort tra Mistral (high) e Qwen/Gemma (low) va dichiarata.
I confronti secondari con Qwen Base devono dichiarare la versione v14 del prompt,
distinta dalla v15 delle nuove prove senza tool.
