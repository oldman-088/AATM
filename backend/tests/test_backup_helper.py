"""E18: the database backup helper used before destructive admin actions."""

import os
import sqlite3
import stat

import pytest

from app.db.backup import BACKUP_KEEP, backup_database


def _make_source(tmp_path):
    src = tmp_path / "source.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO probe (value) VALUES ('kept')")
    conn.commit()
    conn.close()
    return src


def test_backup_is_a_consistent_copy_with_mode_0600(tmp_path):
    src = _make_source(tmp_path)
    backups = tmp_path / "data" / "backups"

    path = backup_database("chat clear", source_path=src, backups_dir=backups)

    assert path.parent == backups
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT value FROM probe").fetchall() == [("kept",)]
    finally:
        conn.close()


def test_backup_name_carries_the_reason(tmp_path):
    src = _make_source(tmp_path)
    path = backup_database("bulk cancel", source_path=src, backups_dir=tmp_path / "b")
    assert "bulk-cancel" in path.name


def test_only_the_newest_twenty_backups_are_kept(tmp_path):
    src = _make_source(tmp_path)
    backups = tmp_path / "b"
    created = [backup_database(f"run {i}", source_path=src, backups_dir=backups) for i in range(BACKUP_KEEP + 3)]

    remaining = sorted(p for p in backups.iterdir() if p.suffix == ".db")
    assert len(remaining) == BACKUP_KEEP
    assert BACKUP_KEEP == 20
    # The oldest three were pruned; the newest survive.
    assert created[-1] in remaining
    assert created[0] not in remaining


def test_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database("x", source_path=tmp_path / "nope.db", backups_dir=tmp_path / "b")
