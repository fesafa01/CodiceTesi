# Dataset MIMIC per il pilota CAMPUS

| Dataset | Stato e ruolo | Collocazione locale |
|---|---|---|
| [MIMIC-IV 2.2, Hosp](https://physionet.org/content/mimiciv/2.2/) | Disponibile. Fonti strutturate per la bozza. | `hosp/` |
| [MIMIC-IV-Note 2.2](https://physionet.org/content/mimic-iv-note/2.2/) | Disponibile. Referti radiologici tra le fonti e lettere di dimissione separate per la valutazione. | `mimic-iv-note-deidentified-free-text-clinical-notes-2.2/note/` |
| [MIMIC-IV 2.2, ICU](https://physionet.org/content/mimiciv/2.2/) | Disponibile. Nove CSV compressi con permanenze, misure ed eventi. | `icu/` |
| [MIMIC-IV-ED 2.2](https://physionet.org/content/mimic-iv-ed/2.2/) | Disponibile. Sei CSV verificati con il manifest PhysioNet. | `mimic-iv-ed-2.2/ed/` |
| [MIMIC-IV-Echo 1.0.1](https://physionet.org/content/mimic-iv-echo/1.0.1/) | Opzionale per i casi cardiologici. Per la bozza bastano le misure strutturate. | Cartella separata per la release 1.0.1 |
| [MIMIC Note Extract 1.0](https://physionet.org/content/mimic-iv-ext-note-extract/1.0/) | Opzionale per la valutazione. Gli estratti derivano dalle lettere originali dello stesso dataset. | Archivio separato dalle fonti di generazione |

I percorsi partono da `$TESI_PRIVATE_DIR/datasets/`, fuori dal workspace del
codice. Il valore predefinito di `TESI_PRIVATE_DIR` è
`~/Desktop/codice-tesi-private`. PostgreSQL conserva il campione
selezionato e le sue lettere di riferimento in un volume Docker locale.

## Download ICU

Usare `curl` e un account
PhysioNet che abbia firmato l'accordo per MIMIC-IV. Sostituire
`TUO_USERNAME_PHYSIONET` con lo username. `curl` chiede la password nel
terminale e invia l'autenticazione Basic fin dalla prima richiesta.

```bash
export TESI_PRIVATE_DIR="${TESI_PRIVATE_DIR:-$HOME/Desktop/codice-tesi-private}"
install -d -m 700 "$TESI_PRIVATE_DIR/datasets/icu"
curl --fail --fail-early --location --continue-at - \
  --basic --user 'TUO_USERNAME_PHYSIONET' \
  --remote-name-all --output-dir "$TESI_PRIVATE_DIR/datasets/icu" \
  'https://physionet.org/files/mimiciv/2.2/icu/{icustays,d_items,caregiver,datetimeevents,ingredientevents,inputevents,outputevents,procedureevents,chartevents}.csv.gz'
```

Se il download si interrompe, rilanciare il comando per riprenderlo.
Se compare `401` o `403`, accedere dal browser alla
[sezione Files della release 2.2](https://physionet.org/content/mimiciv/2.2/#files).
Aprire la cartella `icu` e provare il download di `icustays.csv.gz`. Se
funziona, scaricare gli altri CSV dalla stessa cartella e salvarli in
`$TESI_PRIVATE_DIR/datasets/icu/`. Se anche il browser mostra un rifiuto, controllare
l'accesso a MIMIC-IV e il Data Use Agreement nella pagina del progetto.

Per collegare la degenza ICU, `icustays` associa `subject_id` e `hadm_id`
a `stay_id`. Le tabelle degli eventi si collegano tramite `stay_id` e
`d_items` spiega i codici degli eventi. L'importatore limita la lettura clinica ai ricoveri e alle permanenze
del pilota. Il [protocollo CAMPUS](mimic-campus.md) descrive la selezione.
