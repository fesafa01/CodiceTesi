"""Scadenze, confini e backup sulle sole copie della sandbox locale."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from codice_tesi.retention_demo import (
    MARKER,
    create_backup,
    create_sandbox,
    purge_copies,
    register_database_copy,
    restore_backup,
    write_copy,
)

NOW = datetime(2026, 10, 26, 10, tzinfo=UTC)


def test_deadline_preview_apply_and_repeat_preserve_nonexpired_copies(tmp_path):
    root = tmp_path / "copies"
    create_sandbox(root)
    old = write_copy(root, "artifacts", "old.txt", b"OLD", created_at=NOW - timedelta(days=1))
    fresh = write_copy(root, "artifacts", "fresh.txt", b"FRESH", created_at=NOW)
    before = (root / MARKER).read_bytes()
    preview = asyncio.run(purge_copies(root, as_of=NOW))
    assert preview["planned_files"] == ["artifacts/old.txt"]
    assert old.exists() and fresh.exists() and (root / MARKER).read_bytes() == before
    result = asyncio.run(purge_copies(root, as_of=NOW, apply=True))
    assert result["removed_files"] == ["artifacts/old.txt"]
    assert not old.exists() and fresh.read_bytes() == b"FRESH"
    assert not asyncio.run(purge_copies(root, as_of=NOW, apply=True))["planned_files"]
    assert not asyncio.run(purge_copies(root, as_of=NOW + timedelta(days=1, seconds=-1)))[
        "planned_files"
    ]
    assert asyncio.run(purge_copies(root, as_of=NOW + timedelta(days=1)))["planned_files"]


def test_backup_and_restore_inherit_the_source_deadline(tmp_path):
    root = tmp_path / "copies"
    create_sandbox(root)
    write_copy(root, "sources", "source.json", b"SYNTHETIC", created_at=NOW)
    create_backup(root, "sources/source.json", "backup.json", created_at=NOW + timedelta(days=6))
    restored = restore_backup(
        root, "backups/backup.json", "restored.json", as_of=NOW + timedelta(days=6, hours=1)
    )
    assert restored.read_bytes() == b"SYNTHETIC"
    entries = json.loads((root / MARKER).read_text())["files"]
    assert {entry["expires_at"] for entry in entries} == {(NOW + timedelta(days=7)).isoformat()}
    with pytest.raises(ValueError, match="Backup scaduto"):
        restore_backup(root, "backups/backup.json", "forbidden.json", as_of=NOW + timedelta(days=7))
    asyncio.run(purge_copies(root, as_of=NOW + timedelta(days=7), apply=True))
    assert not restored.exists()
    with pytest.raises(ValueError, match="Backup scaduto"):
        restore_backup(root, "backups/backup.json", "forbidden.json", as_of=NOW + timedelta(days=8))


def test_unmarked_root_and_original_directory_cannot_be_used(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    marker = original / "untouched.txt"
    marker.write_text("KEEP")
    with pytest.raises(FileExistsError):
        create_sandbox(original)
    with pytest.raises(ValueError, match="manifest"):
        asyncio.run(purge_copies(original, as_of=NOW, apply=True))
    assert marker.read_text() == "KEEP"


@pytest.mark.parametrize("name", ["../outside.txt", "/outside.txt", ".."])
def test_paths_outside_inventory_are_rejected(tmp_path, name):
    root = tmp_path / "copies"
    create_sandbox(root)
    with pytest.raises(ValueError):
        write_copy(root, "sources", name, b"DATA", created_at=NOW)


def test_symlinks_and_unregistered_files_stop_deletion_before_changes(tmp_path):
    root = tmp_path / "copies"
    create_sandbox(root)
    old = write_copy(root, "sources", "old.json", b"OLD", created_at=NOW - timedelta(days=8))
    outside = tmp_path / "outside.txt"
    outside.write_text("KEEP")
    link = root / "sources" / "alias"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="Link simbolici"):
        asyncio.run(purge_copies(root, as_of=NOW, apply=True))
    link.unlink()
    unknown = root / "unknown.txt"
    unknown.write_text("UNKNOWN")
    with pytest.raises(ValueError, match="File non censito"):
        asyncio.run(purge_copies(root, as_of=NOW, apply=True))
    assert old.read_bytes() == b"OLD" and outside.read_text() == "KEEP"


def test_database_scope_must_match_before_any_file_is_deleted(tmp_path):
    root = tmp_path / "copies"
    create_sandbox(root)
    old = write_copy(root, "sources", "old.json", b"OLD", created_at=NOW - timedelta(days=8))
    register_database_copy(root, "case-x", created_at=NOW - timedelta(days=8))
    wrong_database = SimpleNamespace(settings=SimpleNamespace(database_schema="public"))
    with pytest.raises(ValueError, match="database della sandbox"):
        asyncio.run(purge_copies(root, as_of=NOW, apply=True, database=wrong_database))
    assert old.exists()


def test_timezone_and_backup_integrity_are_checked(tmp_path):
    root = tmp_path / "copies"
    create_sandbox(root)
    with pytest.raises(ValueError, match="fuso orario"):
        write_copy(root, "sources", "naive.txt", b"DATA", created_at=NOW.replace(tzinfo=None))
    write_copy(root, "sources", "source.json", b"ORIGINAL", created_at=NOW)
    backup = create_backup(root, "sources/source.json", "backup.json", created_at=NOW)
    backup.write_bytes(b"CHANGED")
    with pytest.raises(ValueError, match="Integrità"):
        restore_backup(root, "backups/backup.json", "restored.json", as_of=NOW)
