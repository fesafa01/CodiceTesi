# Giro diagnostico sui tre modelli reali

Sono state usate soltanto fonti inventate con quattro fatti documentali noti.
Nessun campione, bozza clinica o risultato delle campagne è stato aperto.
Prompt, ragionamento e risposte finali non sono salvati: i JSON contengono
soltanto contatori tecnici ed esiti booleani. Queste prove sono guidate e non
entrano nelle 180 esecuzioni sperimentali.

| Modello | Esito del controllo guidato |
|---|---|
| Qwen | 4 chiamate, 4 risultati non vuoti, risposta attesa al primo giro |
| Gemma | 4 chiamate, 4 risultati non vuoti, risposta attesa al primo giro |
| Mistral | Tutti i percorsi dei tool confermati; risposta attesa al terzo giro |

I due precedenti esiti Mistral non conformi sono conservati: nel primo erano
stati chiamati tutti e quattro i tool, nel secondo soltanto tre. I risultati
erano non vuoti e reinviati al modello; la risposta finale non superava il
controllo richiesto. Non si deduce da questi pochi tentativi una percentuale
di affidabilità e non si eliminano gli esiti negativi.

Il terzo controllo usa lo stesso lettore JSON della produzione, che accetta
anche un singolo involucro Markdown JSON. In quel tentativo il modello ha
restituito JSON direttamente: nessun involucro è stato rimosso. Tutti i valori
richiesti della fixture corrispondevano, incluso lo stato originale `Not Given`.
La variabilità osservata non è stata nascosta modificando i dati o i risultati.

Il giro conferma il collegamento tecnico e la capacità di usare risultati non
vuoti sul caso inventato; non misura uso spontaneo dei tool o qualità clinica.
Nella campagna reale vanno conservati anche omissioni, risposte non conformi e
fallimenti, senza retry selettivi che sostituiscano i tentativi iniziali.

La suite da 180 prove non è stata avviata. Le Base restano tutte conservate,
comprese quelle Qwen v14, che non vengono ripetute.
