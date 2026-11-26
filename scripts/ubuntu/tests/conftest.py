"""Shared fixtures for the scripts/ubuntu tests: load a sibling script, build a throwaway database.

The scripts under scripts/ubuntu are stand-alone files, not a package, so they are loaded by path —
the same way the watchdog and the doctor load each other. The database is a temp SQLite file with the
tables the checks read, created here rather than through the backend's models so these tests run
without the project's virtualenv (which is what makes them usable in CI as a fast gate).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]

TABLES = {
    "agents": (
        "id TEXT PRIMARY KEY, role TEXT, status TEXT, current_task_id TEXT, last_heartbeat TEXT,"
        " browser_profile_id TEXT"
    ),
    "tasks": (
        "id TEXT PRIMARY KEY, project_id TEXT, title TEXT, status TEXT, assigned_agent_id TEXT,"
        " task_type TEXT, dependencies TEXT, updated_at TEXT"
    ),
    "arena_sessions": (
        "id TEXT PRIMARY KEY, agent_id TEXT, profile_id TEXT, task_id TEXT, active_task_id TEXT,"
        " project_id TEXT, phase TEXT, is_open INTEGER, page_state TEXT, needs_human_reason TEXT,"
        " error_message TEXT, closed_at TEXT, submitted_at TEXT"
    ),
    "browser_profiles": (
        "id TEXT PRIMARY KEY, name TEXT, agent_id TEXT, connector_status TEXT, needs_human_reason TEXT"
    ),
    "projects": "id TEXT PRIMARY KEY, name TEXT, repo_url TEXT, repo_path TEXT, status TEXT",
    "manager_jobs": "id TEXT PRIMARY KEY, kind TEXT, status TEXT, updated_at TEXT, error TEXT",
}


def load_script(filename: str, module_name: str):
    """Import scripts/ubuntu/<filename> as <module_name>."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TempDB:
    """A throwaway database plus the row helpers the tests need."""

    def __init__(self, path: Path) -> None:
        self.path = path
        conn = sqlite3.connect(path)
        for table, columns in TABLES.items():
            conn.execute(f"CREATE TABLE {table} ({columns})")
        conn.commit()
        conn.close()

    def add(self, table: str, **values: object) -> None:
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn = sqlite3.connect(self.path)
        conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))
        conn.commit()
        conn.close()

    def rows(self, table: str) -> list[dict[str, object]]:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        finally:
            conn.close()

    def snapshot(self) -> dict[str, list[dict[str, object]]]:
        return {table: self.rows(table) for table in TABLES}

    def bytes(self) -> bytes:
        return self.path.read_bytes()


@pytest.fixture
def db(tmp_path: Path) -> TempDB:
    return TempDB(tmp_path / "test.db")


@pytest.fixture
def aatm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``aatm_doctor`` with its backup and log paths pointed at tmp (a test must never touch data/)."""
    monkeypatch.setenv("AATM_DOCTOR_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("AATM_DOCTOR_LOG", str(tmp_path / "doctor.log"))
    module = load_script("aatm_doctor.py", "aatm_doctor_under_test")
    return module


@pytest.fixture
def watchdog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AATM_WATCHDOG_LOG", str(tmp_path / "watchdog.log"))
    monkeypatch.setenv("AATM_WATCHDOG_REOPEN", "0")  # a test must never reload a live Arena window
    return load_script("watchdog.py", "watchdog_under_test")

