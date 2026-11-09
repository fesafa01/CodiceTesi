"""Prepara il limite temporale di un archivio nuovo, senza leggere dati o avviare servizi."""

import argparse
from pathlib import Path


def prepare_archive(root):
    root = Path(root).expanduser()
    if root.is_symlink():
        raise ValueError("Usare una directory, non un collegamento simbolico.")
    root = root.resolve()
    repo = Path(__file__).resolve().parents[1]
    if root.is_relative_to(repo):
        raise ValueError("L'archivio deve essere esterno al repository.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = root / ".archive-reset"
    if marker.exists():
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("Marcatore non valido.")
        return False  # Non sposta il cutoff quando si riprende la campagna.
    if any(root.iterdir()):
        raise ValueError("Directory non vuota senza marcatore: scegliere un archivio nuovo.")
    with marker.open("x", encoding="utf-8") as file:
        file.write("Inizio campagna: conservare questo file anche alla ripresa.\n")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        created = prepare_archive(args.archive)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Archivio non preparato: {exc}\n")
    print("Archivio preparato." if created else "Archivio già preparato; cutoff conservato.")


if __name__ == "__main__":
    main()
