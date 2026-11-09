import pytest

from scripts.prepare_campaign_archive import prepare_archive


def test_archive_cutoff_is_created_once_and_survives_resume(tmp_path):
    path = tmp_path / "new-archive"
    assert prepare_archive(path)
    marker = path / ".archive-reset"
    cutoff = marker.stat().st_mtime_ns
    (path / "synthetic-result").mkdir()
    assert not prepare_archive(path)
    assert marker.stat().st_mtime_ns == cutoff


def test_nonempty_archive_without_cutoff_is_not_reused(tmp_path):
    path = tmp_path / "old-archive"
    path.mkdir()
    (path / "synthetic-result").mkdir()
    with pytest.raises(ValueError, match="non vuota"):
        prepare_archive(path)
    assert not (path / ".archive-reset").exists()
