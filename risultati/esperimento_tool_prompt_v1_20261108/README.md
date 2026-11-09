# Pilota sui prompt espliciti: risultati dell'8 novembre 2026

Il pilota comprende quattro nuove generazioni, separate dalle 180 prove principali.
Piano e limiti sono conservati nella sezione Protocollo di questa cartella.
Tutti gli esiti sono conservati; nessun tentativo è stato ripetuto o sostituito.

| Modello | Caso | Chiamate con prompt originale | Chiamate con variante esplicita | Risultato tool | Validazione tecnica nuova bozza |
|---|---|---:|---:|---|---|
| Gemma | Hosp 1 | 0 | 1 | Non vuoto | Superata al primo passaggio |
| Gemma | Hosp e ED 1 | 0 | 1 | Non vuoto | Superata al primo passaggio |
| Mistral | Hosp 1 | 0 | 1 | Non vuoto | Non superata: sintassi JSON |
| Mistral | Hosp e ED 1 | 0 | 1 | Non vuoto | Superata dopo riparazione deterministica |

Ogni tentativo ha eseguito `ricostruisci_sequenza_eventi`, ricevuto il risultato
e avviato una seconda richiesta contenente quel risultato. Nessun errore di
esecuzione dei tool, nessun argomento respinto e nessun risultato vuoto.
Le richieste HTTP confermano quattro tool disponibili e `tool_choice=auto`:
la chiamata è richiesta dalle istruzioni, senza essere forzata dal protocollo.
Tutte le quattro prove hanno anche snapshot hardware acquisiti.

## Che cosa suggerisce il confronto

I modelli possono usare i tool anche nel compito di stesura su questi due
snapshot reali. Nella nuova variante le istruzioni rendono obbligatoria una
verifica preliminare, eliminano la frase sulla facoltatività e qualificano il
JSON come formato della risposta finale dopo la verifica. La procedura è
esplicitata sia nel system sia nella chiusa user; tutte le fonti restano uguali.
Gemma mantiene reasoning low e Mistral high, come nella campagna principale.

Il confronto è coerente con una sensibilità alla formulazione e alle priorità
operative del prompt. Non dimostra quale singola frase abbia causato l'effetto:
sono stati modificati più passaggi insieme e il confronto usa esiti storici,
con temperatura non nulla e cache/ordine non controllati. Due casi per modello
non misurano l'affidabilità generale. Il numero di chiamate ottenute su richiesta
esplicita non rappresenta selezione autonoma dell'utilità del tool.

Il reinserimento dei risultati nella richiesta successiva verifica il percorso
tecnico; non certifica che ogni frase finale li sfrutti correttamente. La
validazione tecnica non misura qualità clinica o benefici rispetto al prompt
originale. In Mistral resta evidente il problema della conformità del JSON.

## Riproducibilità e collocazione

- `riepilogo.json`: dati aggregabili senza testi clinici.
- `scripts/experiment_tool_prompt.py`: runner; cinque controlli offline passati,
  incluso controllo del mantenimento delle fonti e conservazione dei fallimenti.
- Directory privata: `codice-tesi-private/esperimenti/tool-prompt-v1-20261108`.
  Contiene piano fissato, input con hash, copia del runner, protocollo, log,
  richieste e risposte, registri, risultati tool, campioni Spark e verifica finale.
- Campagna principale: 1.605 file originali e 1.605 copie ricontrollati con
  SHA-256 dopo l'esperimento; tutti gli hash coincidono. Anche piano, runner e
  input del pilota coincidono con il manifest iniziale.

Nessun prompt o parametro di produzione è stato modificato. Prima di adottare
la variante come nuova condizione sperimentale servono ulteriori prove fissate
in anticipo e una valutazione delle risposte, mantenendo distinta la campagna
principale già conclusa.

## Protocollo


Obiettivo: verificare se un prompt che distingue esplicitamente verifiche e
stesura induce chiamate effettive ai tool, mantenendo reasoning abilitato.
Queste prove non entrano nella campagna principale da 180 tentativi.

## Piano fissato prima dell'esecuzione

- Modelli: Gemma e Mistral, scelti perché nella campagna principale entrambi
  hanno effettuato zero chiamate. Qwen ha usato i tool in 25/30 casi ed è escluso.
- Casi: Caso Hosp 1 e Caso Hosp e ED 1 per ciascun modello, indipendentemente
  dall'esito originale. Totale: quattro nuove generazioni, una per coppia.
- Confronto: stesso caso e configurazione nella campagna principale archiviata.
  Il confronto storico non controlla ordine, stato della cache o variabilità;
  è un pilota esplorativo, non una dimostrazione causale o una stima di affidabilità.
- Variante `explicit-phases-v1`: rimuove la facoltatività della chiamata, qualifica
  il formato JSON come vincolo della risposta finale e richiede esplicitamente
  `ricostruisci_sequenza_eventi(limit=3)` prima della stesura. Cambiano istruzioni
  operative in system e nella chiusa user; il catalogo e i testi delle fonti
  restano identici. Le modifiche costituiscono un unico pacchetto di prompt,
  non consentono di attribuire un effetto a una sola frase.
- Parametri: configurazione originale di ciascun caso, incluso reasoning low
  per Gemma e high per Mistral. `tool_choice=auto`, nessuna chiamata forzata dal
  protocollo, nessuna modifica a modelli, schemi, budget e fonti. Un limite esterno
  di 1.200 secondi interrompe ogni prova diagnostica e viene registrato.
- Ogni esito viene conservato, senza ripetere selettivamente tentativi negativi.
  Cinque secondi separano i tentativi. Un'esecuzione interrotta non viene
  automaticamente sovrascritta.

## Misure e artefatti

Esito principale: presenza di almeno una chiamata, verificata anche nel corpo
HTTP effettivamente inviato. Controlli: schemi disponibili, tool_choice, formato
JSON del serving, contatori delle chiamate, errori, risultati vuoti/non vuoti e
reinserimento dei risultati nella richiesta successiva. I token e i tempi sono
misurati dal generatore; i campioni hardware sono acquisiti dal monitor usuale.
Si registra anche la validazione tecnica della bozza con il flusso ordinario.

Tutti gli input, prompt, output e risultati tool restano nella directory privata
`codice-tesi-private/esperimenti/tool-prompt-v1-20261108`. Log e riepilogo non
contengono testi clinici, argomenti dei tool o ragionamento. Gli artefatti privati
conservano richiesta e risposta finale per rendere la prova riesaminabile.
I registri sono in una sottodirectory dell'esperimento, fuori dai percorsi
scansionati dall'archiviatore delle campagne.

Il runner è `scripts/experiment_tool_prompt.py`; va eseguito nell'ambiente del
generatore con un piano privato già validato. Non modifica il prompt di produzione.

## Esito del primo pilota

Concluso l'8 novembre 2026: quattro chiamate riuscite e non vuote su quattro
generazioni, con risultato reinviato al modello. Gemma supera la validazione
tecnica in entrambi i casi; Mistral in uno su due dopo riparazione deterministica.
Il resoconto e i limiti sono riportati sopra.
