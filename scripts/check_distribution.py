"""Verifica una revisione Git e crea un archivio senza materiali clinici privati."""

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT_FILES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    ".python-version",
    "Dockerfile",
    "README.md",
    "compose.yaml",
    "pyproject.toml",
    "uv.lock",
}
DIRECTORIES = {
    ".github",
    "docker",
    "observability",
    "risultati",
    "scripts",
    "src",
    "tests",
    "web",
}
SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".sql",
    ".sh",
    ".txt",
    ".ts",
    ".tsx",
    ".css",
    ".mjs",
}
SPECIAL_FILES = {
    "observability/.env.example",
    "web/.dockerignore",
    "web/Dockerfile",
    "web/bun.lock",
    "web/public/.gitkeep",
    "src/codice_tesi/discharge/tokenizers/LICENSE.deepseek",
    "src/codice_tesi/discharge/tokenizers/LICENSE.qwen",
}
TOKENIZERS = {
    "src/codice_tesi/discharge/tokenizers/deepseek_v4.json": (
        "8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf"
    ),
    "src/codice_tesi/discharge/tokenizers/qwen3_8.json": (
        "0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3"
    ),
}
PRIVATE_PARTS = {
    "data",
    "datasets",
    "var",
    "output",
    "logs",
    "hosp",
    "icu",
    "note",
    "physionet.org",
    "codice-tesi-private",
    "node_modules",
    ".next",
    ".venv",
}
PRIVATE_NAMES = {
    "access.env",
    "scheda.json",
    "metriche.json",
    "bozza.txt",
    "evidenze.md",
    "lettera_ufficiale.txt",
    "risultati_tool.json",
    "risposta.parziale.json",
    "catalogo_evidenze.json",
    "fatti_documentali.json",
    "bozza_strutturata.json",
}
PRIVATE_PATTERNS = (
    re.compile(r"\bmimic-[12]\d{7}\b", re.I),
    re.compile(r"\b[12]\d{7}-(?:DS|RR)-\d+\b"),
    re.compile(r"(?m)^\s*(?:subject_id|hadm_id),.*(?:hadm_id|admittime|charttime|text)"),
    re.compile(
        r"(?mi)^\s*(?:Admission Date|Discharge Date|Name):[^\n]+\n"
        r"\s*(?:Admission Date|Discharge Date|Unit No|Sex):"
    ),
    re.compile(r"APP_MIMIC_TOKEN\s*=\s*['\"]?[A-Za-z0-9_-]{43,}"),
    re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{30,})\b"
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
PRIVATE_RESULT_KEYS = {
    "subject_id",
    "patient_id",
    "hadm_id",
    "episode_id",
    "context_id",
    "note_id",
    "text",
    "prompt",
    "response",
    "content",
    "sources",
    "evidence",
    "reasoning",
}


def validate_file(name: str, raw: bytes, mode: str = "100644") -> None:
    path = PurePosixPath(name)
    if mode not in {"100644", "100755"} or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Tipo di file non distribuibile: {name}")
    if (
        any(
            part.lower() in PRIVATE_PARTS
            or part.lower().startswith(("test-spark", "mimic-iv"))
            or (part.startswith(".env") and name not in ROOT_FILES | SPECIAL_FILES)
            for part in path.parts
        )
        or path.name in PRIVATE_NAMES
        or path.name.startswith("campagna-")
    ):
        raise ValueError(f"Percorso privato non distribuibile: {name}")
    if name not in ROOT_FILES | SPECIAL_FILES and (
        len(path.parts) < 2 or path.parts[0] not in DIRECTORIES or path.suffix not in SUFFIXES
    ):
        raise ValueError(f"File fuori dal perimetro di distribuzione: {name}")
    if name in TOKENIZERS:
        if hashlib.sha256(raw).hexdigest() != TOKENIZERS[name]:
            raise ValueError(f"Tokenizer diverso dall'originale verificato: {name}")
        return
    if len(raw) > 2_000_000 or b"\0" in raw:
        raise ValueError(f"File binario o troppo grande: {name}")
    text = raw.decode("utf-8")
    if any(pattern.search(text) for pattern in PRIVATE_PATTERNS):
        # Non stampare mai il valore che ha fatto scattare il controllo.
        raise ValueError(f"Possibile contenuto privato: {name}")
    if path.parts[0] == "risultati" and path.suffix == ".json":
        check_result(json.loads(text), name)


def check_result(value, name):
    if isinstance(value, dict):
        if PRIVATE_RESULT_KEYS & value.keys():
            raise ValueError(f"Contenuto per caso o testo non ammesso nel riepilogo: {name}")
        for child in value.values():
            check_result(child, name)
    elif isinstance(value, list):
        for child in value:
            check_result(child, name)


def distribution(ref: str, output: Path | None = None) -> int:
    root = Path(__file__).resolve().parents[1]

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])

    commit = git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    entries = git("ls-tree", "-r", "-z", commit).split(b"\0")
    files = []
    for entry in filter(None, entries):
        metadata, name = entry.decode().split("\t", 1)
        mode, kind, sha = metadata.split()
        if kind != "blob":
            raise ValueError(f"Oggetto non distribuibile: {name}")
        raw = git("cat-file", "blob", sha)
        validate_file(name, raw, mode)
        files.append((name, raw, mode))
    content = {
        name: raw.decode()
        for name, raw, _ in files
        if name in {"pyproject.toml", "uv.lock", "web/package.json"}
    }
    version = tomllib.loads(content["pyproject.toml"])["project"]["version"]
    packages = tomllib.loads(content["uv.lock"])["package"]
    locked = next(p["version"] for p in packages if p["name"] == "codice-tesi")
    if version != locked or version != json.loads(content["web/package.json"])["version"]:
        raise ValueError("Versioni Python, frontend e lockfile incoerenti.")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        prefix = output.stem
        # I dati sono già tutti validati: nessun archivio parziale su errore di controllo.
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, raw, mode in files:
                info = zipfile.ZipInfo(f"{prefix}/{name}", (2026, 1, 1, 0, 0, 0))
                info.external_attr = int(mode, 8) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, raw)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n")
    print(f"Distribuzione verificata: {len(files)} file, commit {commit[:12]}.")
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        distribution(args.ref, args.output)
    except (ValueError, UnicodeError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Distribuzione bloccata: {exc}\n")


if __name__ == "__main__":
    main()
