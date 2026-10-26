"""Scadenze dimostrative applicate solo a copie locali esplicitamente censite.

Nessuna scansione degli output ordinari e nessun job automatico. La rimozione
dei file è logica: non garantisce cancellazione fisica da SSD, WAL o snapshot.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

MARKER = ".retention-demo.json"
DAYS = {"sources": 7, "artifacts": 1, "telemetry": 1, "registry": 30, "backups": 7}


def instant(value: datetime | str) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("Usare date con fuso orario esplicito.")
    return result.astimezone(UTC)


def create_sandbox(root: Path) -> str:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    identity = uuid4().hex
    _save(root, {"version": "1.0", "demo_id": identity, "files": [], "database": []})
    return "retention_demo_" + identity


def _save(root, manifest):
    temporary = root / (MARKER + ".tmp")
    # Non seguire un eventuale link lasciato nel percorso temporaneo.
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(root / MARKER)


def _child(root, relative):
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Percorso fuori dalla sandbox.")
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Percorso fuori dalla sandbox.")
    if any((root / Path(*path.parts[:i])).is_symlink() for i in range(1, len(path.parts) + 1)):
        raise ValueError("Link simbolici non ammessi nella sandbox.")
    return target


def _load(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or (root / MARKER).is_symlink():
        raise ValueError("Sandbox dimostrativa non valida.")
    try:
        manifest = json.loads((root / MARKER).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("Manca un manifest dimostrativo valido.") from exc
    identity = manifest.get("demo_id", "")
    if (
        manifest.get("version") != "1.0"
        or len(identity) != 32
        or any(char not in "0123456789abcdef" for char in identity)
    ):
        raise ValueError("Manifest dimostrativo non valido.")
    known = {MARKER}
    for entry in manifest["files"]:
        category = entry["category"]
        if category not in DAYS or Path(entry["path"]).parts[0] != category:
            raise ValueError("Categoria o percorso non valido.")
        target = _child(root, entry["path"])
        if entry["path"] in known:
            raise ValueError("Percorso duplicato nel manifest.")
        known.add(entry["path"])
        created, expires = instant(entry["created_at"]), instant(entry["expires_at"])
        if not created <= expires <= created + timedelta(days=DAYS[category]):
            raise ValueError("Scadenza fuori dalla politica dimostrativa.")
        if entry["deleted_at"] is not None and target.exists():
            raise ValueError("Un file già cancellato è ricomparso nella sandbox.")
        if target.exists() and not target.is_file():
            raise ValueError("Sono ammessi soltanto file regolari.")
    for entry in manifest["database"]:
        if entry["schema"] != "retention_demo_" + identity:
            raise ValueError("Schema PostgreSQL fuori dalla sandbox.")
        if instant(entry["expires_at"]) != instant(entry["created_at"]) + timedelta(days=7):
            raise ValueError("Scadenza DB non coerente con la politica.")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Link simbolici non ammessi nella sandbox.")
        if path.is_file() and path.relative_to(root).as_posix() not in known:
            raise ValueError("File non censito nella sandbox: operazione interrotta.")
    return manifest


def write_copy(root, category, name, content: bytes, *, created_at, expires_at=None):
    root = Path(root)
    manifest = _load(root)
    if category not in DAYS or Path(name).name != name or not name or name in {".", ".."}:
        raise ValueError("Categoria o nome della copia non valido.")
    created = instant(created_at)
    expiry = instant(expires_at) if expires_at else created + timedelta(days=DAYS[category])
    if not created <= expiry <= created + timedelta(days=DAYS[category]):
        raise ValueError("Scadenza fuori dalla politica dimostrativa.")
    relative = category + "/" + name
    if any(entry["path"] == relative for entry in manifest["files"]):
        raise ValueError("Nome già censito; usare una nuova copia.")
    target = _child(root, relative)
    target.parent.mkdir(exist_ok=True)
    with target.open("xb") as stream:
        stream.write(content)
    manifest["files"].append(
        {
            "path": relative,
            "category": category,
            "created_at": created.isoformat(),
            "expires_at": expiry.isoformat(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "deleted_at": None,
        }
    )
    _save(root, manifest)
    return target


def register_database_copy(root, context_id, *, created_at):
    root = Path(root)
    manifest = _load(root)
    if any(entry["context_id"] == context_id for entry in manifest["database"]):
        raise ValueError("Contesto già censito.")
    created = instant(created_at)
    manifest["database"].append(
        {
            "schema": "retention_demo_" + manifest["demo_id"],
            "context_id": context_id,
            "created_at": created.isoformat(),
            "expires_at": (created + timedelta(days=7)).isoformat(),
            "deleted_at": None,
        }
    )
    _save(root, manifest)


def create_backup(root, source_path, name, *, created_at):
    root = Path(root)
    manifest = _load(root)
    source = next(entry for entry in manifest["files"] if entry["path"] == source_path)
    created = instant(created_at)
    if (
        source["category"] != "sources"
        or source["deleted_at"]
        or not instant(source["created_at"]) <= created < instant(source["expires_at"])
    ):
        raise ValueError("La fonte non è disponibile per il backup.")
    payload = _child(root, source_path).read_bytes()
    if hashlib.sha256(payload).hexdigest() != source["sha256"]:
        raise ValueError("Integrità della copia non valida.")
    return write_copy(
        root,
        "backups",
        name,
        payload,
        created_at=created,
        expires_at=min(instant(source["expires_at"]), created + timedelta(days=7)),
    )


def restore_backup(root, backup_path, name, *, as_of):
    root = Path(root)
    manifest = _load(root)
    backup = next(entry for entry in manifest["files"] if entry["path"] == backup_path)
    now = instant(as_of)
    if (
        backup["category"] != "backups"
        or backup["deleted_at"]
        or not instant(backup["created_at"]) <= now < instant(backup["expires_at"])
    ):
        raise ValueError("Backup scaduto o cancellato: ripristino rifiutato.")
    payload = _child(root, backup_path).read_bytes()
    if hashlib.sha256(payload).hexdigest() != backup["sha256"]:
        raise ValueError("Integrità del backup non valida.")
    return write_copy(
        root, "sources", name, payload, created_at=now, expires_at=backup["expires_at"]
    )


async def purge_copies(root, *, as_of, apply=False, database=None):
    root = Path(root)
    manifest = _load(root)
    now = instant(as_of)
    files = [
        e for e in manifest["files"] if e["deleted_at"] is None and instant(e["expires_at"]) <= now
    ]
    records = [
        e
        for e in manifest["database"]
        if e["deleted_at"] is None and instant(e["expires_at"]) <= now
    ]
    result = {
        "as_of": now.isoformat(),
        "applied": apply,
        "planned_files": [entry["path"] for entry in files],
        "planned_contexts": [entry["context_id"] for entry in records],
        "removed_files": [],
        "already_missing_files": [],
        "deleted_sources": 0,
        "deleted_contexts": 0,
    }
    if not apply:
        return result
    if records:
        expected_schema = "retention_demo_" + manifest["demo_id"]
        if database is None or database.settings.database_schema != expected_schema:
            raise ValueError("Serve il database della sandbox dimostrativa.")
        host = urlsplit(database.settings.database_url.get_secret_value()).hostname
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Il database dimostrativo deve essere locale.")
        ids = [entry["context_id"] for entry in records]
        async with database.connection() as connection:
            sources = await connection.execute(
                "DELETE FROM composition_sources WHERE context_id = ANY(%s)", (ids,)
            )
            contexts = await connection.execute(
                "DELETE FROM composition_contexts WHERE id = ANY(%s)", (ids,)
            )
            result["deleted_sources"], result["deleted_contexts"] = (
                sources.rowcount,
                contexts.rowcount,
            )
        for entry in records:
            entry["deleted_at"] = now.isoformat()
    for entry in files:
        target = _child(root, entry["path"])
        if target.exists():
            target.unlink()
            result["removed_files"].append(entry["path"])
        else:
            result["already_missing_files"].append(entry["path"])
        entry["deleted_at"] = now.isoformat()
    _save(root, manifest)
    return result
