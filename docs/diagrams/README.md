# Applicazione attuale — UML

Stato del 17 ottobre 2026: avvio senza dati clinici, schema SQL e accesso PostgreSQL tramite pool Psycopg.

- [Componenti applicative](Applicazione_Componenti.png): agente CLI, client LLM,
  API cliniche/FHIR, repository, persistenza, dataset, gestione e test.
- [Distribuzione](Applicazione_Deployment.png): container Compose, ruoli database,
  volume del database, input esterno per l'importazione esplicita e server di inferenza esterno.
- [Draw.io modificabile, due pagine](Applicazione_Attuale.drawio).
- Esportazioni vettoriali: [componenti](Applicazione_Componenti.svg),
  [distribuzione](Applicazione_Deployment.svg).

Notazione: diagramma di componenti e diagramma di deployment UML. Il riferimento
è [OMG UML 2.5.1](https://www.omg.org/spec/UML/2.5.1/About-UML). Le dipendenze vanno
dall'utilizzatore al fornitore. Le linee di comunicazione tra ambienti non indicano
l'ordine temporale delle chiamate. I file draw.io sono documenti mxGraph con
notazione UML, non un metamodel esportato in XMI.

L'agente non ha ancora tool clinici registrati. L'interfaccia presente nel backend
è Swagger per esplorare le API; non è un'interfaccia clinica per il medico.
I servizi amministrativi e l'agente CLI si avviano su richiesta. Il nodo GitHub
Actions rappresenta il workflow configurato, senza attestare esecuzioni remote.
Il server LLM e la sua disponibilità non vengono simulati da Compose.
Il repository è utilizzabile direttamente da Python, anche senza HTTP; i futuri
tool dell’agente non sono ancora collegati, quindi non viene disegnata una
dipendenza attiva agente-database. PostgreSQL è pubblicato solo su localhost:5433.

Rigenerazione dalla radice di `CodiceTesi/`:

```bash
PYTHONPATH=src uv run --locked --extra backend --no-editable python scripts/application_diagrams.py
```

Il generatore produce draw.io, SVG e PNG dalle stesse coordinate. Prima di
rigenerare dopo modifiche manuali al draw.io, riportare le modifiche nel generatore.
Queste viste descrivono l'applicazione implementata; i diagrammi del processo
ospedaliero appartengono ai materiali in `policlinico/`.
