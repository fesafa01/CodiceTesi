#!/usr/bin/env bash
# Aggiunta separata dei candidati clinici; nessuna sostituzione del campione.
set -euo pipefail

clinical_project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$clinical_project_dir"
export TESI_PRIVATE_DIR="${TESI_PRIVATE_DIR:-$(cd .. && pwd)/codice-tesi-private}"

if [[ ! -f "$TESI_PRIVATE_DIR/var/mimic/access.env" ]]; then
  echo "Manca il file di accesso del campione precedente: var/mimic/access.env." >&2
  exit 2
fi

docker compose build backend-admin
docker compose up -d --wait db
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
  -v "$TESI_PRIVATE_DIR/datasets/hosp:/mimic/hosp:ro" \
  -v "$TESI_PRIVATE_DIR/datasets/mimic-iv-note-deidentified-free-text-clinical-notes-2.2/note:/mimic/note:ro" \
  -v "$TESI_PRIVATE_DIR/datasets/mimic-iv-ed-2.2/ed:/mimic/ed:ro" \
  -v "$TESI_PRIVATE_DIR/var/mimic:/mimic-private" \
  backend-admin add-clinical-candidates /mimic/hosp /mimic/note \
    --ed /mimic/ed --limit 10 --seed 20261109 \
    --min-input-tokens 0 --max-input-tokens 80000 --pool-per-profile 30 \
    --expect-existing 30 \
    --report-dir /mimic-private/validazione-clinica-20261109 \
    --access-file /mimic-private/access.env "$@"

for clinical_arg in "$@"; do
  if [[ "$clinical_arg" == "--prepare-only" ]]; then
    exit 0
  fi
done

# env_file è acquisito alla creazione del container: il solo restart non lo rilegge.
docker compose up -d --no-deps --force-recreate --wait \
  backend reference-api agent-api web spark-archive

echo "Report: $TESI_PRIVATE_DIR/var/mimic/validazione-clinica-20261109/candidati.md"
