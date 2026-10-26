# Registro delle verifiche tecniche

Sessioni del 26 ottobre 2026, con materiale sintetico provvisorio e fixture. Non rappresentano la valutazione clinica delle bozze. Le evidenze complete rimangono locali in `output/verifiche/sovranita/`, escluse da Git; questa pagina ne conserva scopo, risultati e limiti.

| Sessione | Riscontri | Limite |
|---|---|---|
| Baseline | 131 test superati, 10 integrazioni SQL saltate; 60 originali e 30 file indicizzati con hash coerenti; 30 pacchetti validi; ruolo SQL di lettura confermato; preparazione e rifiuto del budget verificati. | Le richieste anonime ricevevano 200: lacuna di accesso poi corretta. Nessuna inferenza o prova offline. |
| Accessi demo | 164 test, inclusa integrazione SQL; 175/175 controlli HTTP: 70 risposte 401, 30 risposte 403 e 75 risposte 200; 7/7 dry-run coerenti, senza token negli artefatti. | Identità pubbliche fittizie, non identificazione di persone reali. |
| Ciclo di vita delle copie | 174 test complessivi; anteprima senza modifiche; rimossi selettivamente 21 file, 2 contesti e 36 fonti SQL; backup valido ripristinato senza estensione e backup scaduto respinto; controllo preservato; 98 file di partenza identici. | Cancellazione logica di copie e fixture, non pulizia degli archivi attivi o sovrascrittura fisica. |

I test SQL usano schemi isolati; per le sessioni dedicate sono stati utilizzati PostgreSQL temporanei su loopback, successivamente rimossi. I conteggi di richieste o di ripetizioni non sono pazienti indipendenti. I registri del client e quelli del backend attivo sono stati confrontati nella sessione sugli accessi; le impronte valgono per il rilascio osservato in quel momento.

## Stato per requisito

| Requisito | Stato nel perimetro dichiarato |
|---|---|
| D1, controllo dei dati | Parziale: contratti e impronte verificati; inventario completo e log reali ancora aperti. |
| D2, accessi e ciclo di vita | Sotto-prove demo superate; intero ambiente parziale perché archivi attivi e copie residue non sono tutti coperti. |
| A1, autonomia | Non eseguita su rete controllata con inferenza e riavvio. |
| R1, identità/ripristino | Parziale: codice e configurazioni identificati; archivio completo del serving e ricostruzione non provati. |
| Q1, qualità documentale | Forma e budget verificati; valutazione semantica e clinica non eseguita. |
| C1/C2, contenimento | Privilegi/contratti verificati; istruzioni spurie e arresto reale del serving aperti. |
| P1, portabilità | Predisposta, senza confronto eseguito su alternativa locale. |
| S1/S2, sostenibilità | Contratto delle misure e guasti simulati verificati; carico, risorse e recupero reali aperti. |

## Ripetizione

Le procedure e i criteri sono in [sovranità](sovranita.md). Eseguire i verificatori con una destinazione nuova sotto `output/verifiche/`. Per la suite completa impostare `CLINICAL_TEST_POSTGRES_URL` su PostgreSQL di test; senza questa variabile i test SQL sono saltati. Non interpretare uno skip come prova superata.

L'autonomia senza Internet non deriva dai dry-run; la cancellazione di copie non dimostra il ripristino dell'intero ambiente; il formato corretto non prova fedeltà clinica. Non viene calcolato un punteggio globale di sovranità o conformità.
