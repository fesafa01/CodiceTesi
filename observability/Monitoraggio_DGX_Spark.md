# Raccolta delle misure sul DGX Spark

## Raccolta automatica a fine prova

Per le generazioni avviate sul DGX Spark da CLI o da CAMPUS, il workflow salva
automaticamente `spark/RUN_ID.json` nella stessa cartella di output che contiene
`registro/RUN_ID.json`. Per esempio, in CAMPUS con MIMIC il file è in
`output/dimissioni-mimic/spark/`. Nel registro, `spark_monitor` indica il percorso
relativo all'output, lo stato di raccolta e quanti punti ricadono nella prova
quando lo storico è disponibile. Il file include anche le risposte del monitor
per la finestra richiesta, di almeno 15 minuti e più ampia per le prove lunghe.

La raccolta avviene dopo la chiusura della prova e include i tentativi di
inferenza falliti o interrotti. Le anteprime `--dry-run` e le prove su altri
host saltano la raccolta. Un endpoint in errore è indicato in `errors`; i dati
ricevuti dall'altro endpoint restano disponibili. La richiesta invia solo la
finestra temporale al monitor. Il `run_id` e l'associazione restano locali.

## Raccolta manuale via API

Dalla radice `CodiceTesi`, con Tailscale connesso:

```bash
python3 observability/spark_usage.py
python3 observability/spark_usage.py --run output/dimissioni/registro/RUN_ID.json
```

Nel secondo comando, sostituire `RUN_ID` con l'identificativo di una prova
conclusa e lanciare il comando appena termina. Lo script interroga `/history`
e `/energy` su `http://spark-413e.tail9c981a.ts.net:9108` e salva entrambe le
risposte in un JSON con timestamp UTC in `output/metriche/spark/`. Con `--run`
aggiunge `run.run_id` e `run.series`, che contiene i punti con timestamp compreso
fra `started_at` ed `ended_at`. Lo script usa la libreria standard di Python.

La finestra predefinita è di 24 ore per la raccolta generale e 15 minuti per
la singola prova. Per una prova più lunga o conclusa da più tempo, usare
`--hours` con un valore che risalga fino a prima dell'inizio, per esempio
`--hours 0.5`. Controllare `history.step` e gli orari dei punti restituiti.
Nella verifica sullo Spark, il passo era di 10 secondi su 15 minuti e di
172 secondi su 24 ore. Una serie vuota indica assenza di punti nell'intervallo.

`energy.wh_total` riguarda l'intera finestra mobile di `/energy`. Usare
`energy.power_source_samples` per verificare quali sorgenti di potenza hanno
contribuito. Nelle risposte verificate i campioni provenivano dalla GPU.

## Raccolta manuale

Procedura per la durata delle prove, con comandi da eseguire a mano. Si usano
`nvidia-smi`, `top` e il server di file incluso in Python 3. La GPU e la RAM
vengono campionate ogni 5 secondi sullo Spark, anche quando il Mac è scollegato.
I file sono scaricabili tramite la porta TCP 9401 su Tailscale.

## 1. Verifica sullo Spark

Aprire un terminale sullo Spark con l'utente normale, direttamente sul sistema
e fuori dal container di inferenza. Eseguire una riga alla volta.

```bash
tailscale ip -4
python3 --version
date -u
timedatectl show -p NTPSynchronized
```

Annotare l'IPv4 Tailscale. Verificare che l'ora sia sincronizzata anche sul Mac
prima di allineare i campioni agli orari delle prove.

```bash
TZ=UTC nvidia-smi --query-gpu=timestamp,index,utilization.gpu,temperature.gpu,clocks.gr --format=csv
LC_ALL=C TZ=UTC top -b -n 1 -E m -p 0
```

Il primo comando mostra utilizzo GPU, temperatura e clock grafico. Se un campo
risulta `N/A`, trattarlo come dato mancante. Se il driver segnala un nome di campo
sconosciuto, controllare `nvidia-smi --help-query-gpu` e togliere quel campo dalla
query di prova e da quella periodica. Nell'output di `top`, verificare la
presenza di `MiB Mem`, `avail Mem` e `MiB Swap`.

```bash
mktemp -d "$HOME/spark-misure.XXXXXX"
```

Annotare il percorso assoluto stampato dal comando. Usare questa cartella, appena
creata e dedicata ai due file, in tutti i passaggi successivi.

## 2. Avvio della raccolta sullo Spark

**Terminale 1.** Sostituire `PERCORSO_CARTELLA` con il percorso appena ottenuto.
Il comando rimane attivo fino a `Ctrl+C`.

```bash
cd "PERCORSO_CARTELLA" && TZ=UTC stdbuf -oL nvidia-smi \
  --query-gpu=timestamp,index,utilization.gpu,temperature.gpu,clocks.gr \
  --format=csv --loop=5 > gpu.csv
```

**Terminale 2.** Aprire un altro terminale sullo Spark e usare lo stesso
percorso. `top` registra il riepilogo dell'host e mostra nella tabella dei
processi soltanto `top` stesso. La prima riga del file indica la data di avvio
in UTC. Ogni riepilogo successivo ha l'ora UTC. Al passaggio della mezzanotte
UTC, i campioni successivi appartengono al giorno seguente.

```bash
cd "PERCORSO_CARTELLA" && date -u +%FT%TZ > memoria.log
LC_ALL=C TZ=UTC stdbuf -oL top -b -d 5 -E m -p 0 >> memoria.log
```

Eseguire la seconda riga soltanto dopo che la prima è riuscita. I comandi di
avvio creano i file da zero. Per una nuova sessione ripetere `mktemp -d`.

## 3. Lettura dal Mac via Tailscale

**Terminale 3 sullo Spark.** Prima verificare che i file contengano più campioni.

```bash
cd "PERCORSO_CARTELLA"
tail gpu.csv
tail -n 16 memoria.log
```

Il server HTTP integrato in Python serve tutta la cartella indicata e non ha un
login proprio. Avviarlo per la finestra di prova solo se la policy Tailscale
consente TCP 9401 sullo Spark ai soli client autorizzati. Usare l'IPv4 Tailscale
annotato al punto 1 al posto di `IP_TAILSCALE_SPARK`.

```bash
python3 -m http.server 9401 --bind IP_TAILSCALE_SPARK --directory "PERCORSO_CARTELLA"
```

**Sul Mac con Tailscale connesso.** Usare lo stesso IP. Ripetendo i comandi dopo
qualche secondo, gli orari in coda devono avanzare.

```bash
curl -fsS --max-time 15 http://IP_TAILSCALE_SPARK:9401/gpu.csv | tail -n 5
curl -fsS --max-time 15 http://IP_TAILSCALE_SPARK:9401/memoria.log | tail -n 16
```

Se il Mac non raggiunge la porta, usare `ss -ltn 'sport = :9401'` sullo Spark
per verificare che il server sia in ascolto all'indirizzo Tailscale. Controllare
che la policy della rete e il firewall dell'host permettano il collegamento.
Si può continuare a raccogliere sullo Spark e trasferire i due file a fine prova
tramite un canale già autorizzato.

## 4. Fine della prova

Premere `Ctrl+C` nei terminali 1 e 2. Lasciare il server attivo per il download
finale. Sul Mac, da una cartella dedicata alla prova, eseguire:

```bash
curl -fsS --max-time 60 -o gpu.csv http://IP_TAILSCALE_SPARK:9401/gpu.csv
curl -fsS --max-time 60 -o memoria.log http://IP_TAILSCALE_SPARK:9401/memoria.log
```

Dopo il download, premere `Ctrl+C` nel terminale 3. I file originali restano
nella cartella dedicata sullo Spark. Durante la prova, i tre processi restano
attivi nei rispettivi terminali.

## Lettura delle misure

- Il DGX Spark usa RAM condivisa tra CPU e GPU. Usare `avail Mem` per la memoria
  disponibile e `total − avail Mem` come stima della memoria usata sull'intero
  host. Leggere a parte lo swap usato. Per attribuire le allocazioni al singolo
  modello serve una misura dal runtime di inferenza.
- Annotare l'intervallo a riposo prima della prova. Per confrontare le prove,
  allineare i campioni in UTC con `started_at` ed `ended_at` del registro della
  tesi. Su un host condiviso, anche altri carichi contribuiscono alle misure.

[Guida NVIDIA sulla memoria unificata](https://docs.nvidia.com/dgx/dgx-spark/known-issues.html) ·
[Riferimento di `nvidia-smi`](https://docs.nvidia.com/deploy/nvidia-smi/) ·
[Documentazione di `top`](https://gitlab.com/procps-ng/procps/-/blob/master/man/top.1) ·
[Riferimento Linux per `MemAvailable`](https://docs.kernel.org/filesystems/proc.html) ·
[Server di file Python](https://docs.python.org/3/library/http.server.html) ·
[Controllo accessi Tailscale](https://tailscale.com/docs/features/access-control)
