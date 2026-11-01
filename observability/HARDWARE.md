# Risorse hardware dello Spark

## Stato e perimetro — 30 ottobre 2026

La raccolta è limitata a CPU, memoria di sistema condivisa, swap, disco e tre
misure GPU: utilizzo, temperatura e clock grafico. Sono stati rimossi framebuffer
GPU, attività/clock della memoria GPU e tutte le letture di potenza. Non si
raccolgono energia o costi e non si ricavano questi valori da token o durata.

**Nessun sensore è ancora verificato sullo Spark reale.** Le ultime verifiche
remote, del 28 ottobre, riportano API LLM raggiungibile sulla porta 8000,
`/metrics` del serving con HTTP 404 e porta 11000 non raggiungibile direttamente.
L'operatore non dispone di SSH. Non sono stati modificati servizi o driver.
I test locali usano fixture: il primo `--once` sullo Spark servirà a confermare
quali misure essenziali restituisce effettivamente il driver installato.

## Misure mantenute

| Fonte | Metriche |
|---|---|
| `/proc/stat`, `/proc/loadavg` | CPU occupata (idle e iowait esclusi), CPU logiche, load a 1 minuto |
| `/proc/meminfo` | Memoria totale, disponibile e usata (`MemTotal - MemAvailable`), swap usata |
| filesystem `/` | Capacità e spazio libero del filesystem radice |
| `nvidia-smi -q -x` | Utilizzo GPU, temperatura GPU, clock grafico |

La memoria dello Spark è condivisa fra CPU e GPU: la misura Linux riguarda
l'intero sistema, non la sola memoria del modello. NVIDIA documenta che il
framebuffer dedicato non è supportato su queste iGPU. Non si sommano RAM e una
presunta VRAM separata. La potenza è esclusa dal perimetro essenziale scelto,
anche quando il driver ne restituisce un valore: questo non significa che ogni
lettura di potenza GPU sia tecnicamente impossibile.

Valori `N/A` o `Not Supported` sono omessi; uno zero numerico resta zero.
`spark_hardware_source_available{source="gpu"}` vale 1 se almeno una delle tre
misure GPU è leggibile. Non certifica la disponibilità di tutti e tre i campi:
controllare anche l'elenco `metrics` dell'inventario.

## Cosa chiedere al professore

È sufficiente avviare un piccolo programma in lettura **direttamente sullo Spark**,
con l'utente normale della macchina e fuori dal container LLM. Il file è
`src/codice_tesi/observability/hardware_probe.py`: trasferire solo questo file con
un mezzo già autorizzato. Usa la libreria standard di Python **3.11 o successivo**;
per la GPU usa `nvidia-smi` già presente sullo Spark. Non richiede il progetto,
`pip`, Docker o privilegi root. Il codice non modifica il serving o la GPU.

1. Aprire un terminale sullo Spark, nella cartella contenente il file. Verificare
   Python, indirizzo Tailscale e sincronizzazione dell'orologio:

   ```bash
   python3 --version
   tailscale ip -4
   timedatectl show -p NTPSynchronized
   ```

   Sono necessari orologi sincronizzati sullo Spark e sul Mac. Se il controllo
   riporta `NTPSynchronized=no`, far verificare la sincronizzazione al gestore
   della macchina prima delle prove.

2. Produrre e restituire l'inventario dei sensori:

   ```bash
   python3 hardware_probe.py --once > sensori-spark.json
   ```

   Controllare `sources` e `metrics`: CPU, memoria e disco dovrebbero essere
   disponibili; le sole metriche GPU previste sono `spark_gpu_utilization_ratio`,
   `spark_gpu_temperature_celsius`, `spark_gpu_graphics_clock_hertz`.
   `devices` e `driver_version` identificano GPU e driver, se leggibili.
   Se la GPU risulta indisponibile, verificare `nvidia-smi` nello stesso terminale.
   Il file JSON serve a verificare i sensori prima delle inferenze.

3. Lasciare la sonda attiva durante le prove:

   ```bash
   python3 hardware_probe.py --listen 100.97.236.18 --port 9401 --interval 1
   ```

   `100.97.236.18` è l'IP Tailscale annotato il 28 ottobre: usare quello restituito
   al punto 1 se diverso. Tenere aperto il terminale; `Ctrl+C` arresta la sonda.
   Nessuna installazione permanente è necessaria per il primo collaudo.
   Se una policy di rete blocca la connessione, il gestore deve consentire
   **TCP 9401 dai soli client Tailscale autorizzati**.

La sonda ha due endpoint sulla stessa porta: `/snapshot` per il client e
`/metrics` per Prometheus. Il default è `127.0.0.1`; per l'accesso remoto si usa
esclusivamente l'interfaccia Tailscale autorizzata. Non occorre aprire la dashboard
DGX sulla 11000, modificare l'API sulla 8000 o concedere SSH al client.
Il rifiuto della connessione diretta alla 11000 non dimostra un guasto della
dashboard: NVIDIA ne documenta l'accesso remoto tramite tunnel/Sync.

## Verifica dal Mac e attivazione client

Quando il professore ha avviato la sonda:

```bash
curl --fail --max-time 5 http://spark-413e.tail9c981a.ts.net:9401/snapshot
curl --fail --max-time 5 http://spark-413e.tail9c981a.ts.net:9401/metrics
```

Se questi rispondono con campioni recenti, eseguire una generazione con `--hardware`.
Il client e Prometheus sono già configurati per questo hostname e porta. Se
cambiano, aggiornare `HARDWARE_ENDPOINT` in `observability/hardware.env` e il target
in `observability/prometheus.yaml`. Per avviare il client Python dalla radice
`CodiceTesi`, aggiornare l'installazione con `uv sync --locked --extra backend
--no-editable` dopo modifiche al codice.

`hardware.env` è separato dalla configurazione OTLP; le variabili `HARDWARE_*`
nell'ambiente hanno precedenza. Compose abilita già la raccolta. `--no-hardware`
la disabilita; `--no-telemetry` disabilita OTLP ma lascia i campioni locali.
Il dry-run non interroga la sonda.

## Registro e Phoenix

Durante la chiamata LLM il client legge `/snapshot` ogni secondo, con timeout di
un secondo. Rifiuta campioni vecchi di oltre 10 secondi o oltre 5 secondi nel
futuro, schemi non supportati, valori non finiti/negativi e serie duplicate.
Scarta campioni precedenti alla richiesta e duplicati, segnala cambi di
host/riavvii della sonda senza mescolare i dati. La allowlist client esclude
le metriche rimosse anche se riceve campioni da una vecchia sonda.

Il campo `hardware` del registro conserva stato, host, metadati GPU disponibili,
timestamp, errori, numero di campioni, minimo/massimo/media per serie e il percorso:

```text
output/dimissioni/hardware/<run_id>.jsonl
```

La media è aritmetica sui campioni unici. Le risorse sono dell'**intero host
condiviso durante la richiesta**, non esclusivamente della lettera o del processo
LLM. L'intervallo client non certifica l'arresto del lavoro server in caso di
interruzione. Non sono previste analisi energetiche con questa sonda.

Gli stati sono `disabled`, `not_started`, `no_samples`, `unavailable`, `partial`,
`available`. `partial` segnala errori o fonti indisponibili; `available` non
garantisce che ogni singolo sensore GPU abbia risposto. Un guasto della sonda
non cambia l'esito della generazione. Il checkpoint ogni 15 secondi aggiorna il
riepilogo; i campioni già scritti restano su disco anche dopo SIGKILL.
La chiusura può aggiungere fino al timeout della sonda alla fase di generazione;
la durata LLM dell'adattatore resta quella della richiesta al provider.

Phoenix mostra su span radice e LLM `tesi.hardware.status`, `samples`, `host`,
`scope`, `attribution`, `statistics` (JSON con minimo/massimo/media e unità).
I campioni grezzi restano locali; CSV/JSONL del registro includono `hardware`.

## Prometheus

Il job `spark-hardware` interroga la sonda ogni 5 secondi, conserva 90 giorni e
non usa identificativi di esecuzioni o dati clinici nelle etichette. I dettagli
per richiesta, campionati ogni secondo, restano nei JSONL locali.

```promql
up{job="spark-hardware"}
time() - spark_hardware_sample_timestamp_seconds
spark_hardware_source_available{source="gpu"}
spark_gpu_utilization_ratio * 100
spark_gpu_temperature_celsius
spark_gpu_graphics_clock_hertz / 1000000
spark_memory_used_bytes / 1024^3
```

Finché la sonda non è attiva, `up` vale 0 e le misure sono assenti. Per applicare
modifiche alla configurazione, dalla radice `CodiceTesi`:

```bash
docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.yml
docker compose restart prometheus
```

Riferimenti verificati il 30 ottobre 2026:
[NVIDIA: memoria condivisa e framebuffer non supportato](https://docs.nvidia.com/dgx/dgx-spark/known-issues.html),
[query NVIDIA SMI](https://docs.nvidia.com/deploy/nvidia-smi/index.html),
[NVIDIA: accesso alla dashboard DGX](https://docs.nvidia.com/dgx/dgx-spark/dgx-dashboard.html).
