"""scripts/ubuntu/reset-admin-password.sh reports a verified result and never leaks the password."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest
from app.core.security import hash_password, verify_password

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ubuntu" / "reset-admin-password.sh"
EMAIL = "admin@aatm.local"
PASSWORD = "test-only-password-123"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "reset.db"
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.close()
    subprocess.run([str(ROOT / ".venv/bin/alembic"), "upgrade", "head"], cwd=ROOT / "backend", env=_env(path, tmp_path),
                   check=True, capture_output=True)
    con = sqlite3.connect(path)
    columns = {row[1]: row for row in con.execute("pragma table_info(users)")}
    values = {"id": "u1", "email": EMAIL, "full_name": "Admin", "password_hash": hash_password("old-password-value"),
              "role": "admin", "language": "ar", "theme": "dark", "is_active": 1, "must_change_password": 0,
              "token_version": 2, "created_at": "2026-01-01 00:00:00"}
    values = {key: value for key, value in values.items() if key in columns}
    con.execute(f"insert into users ({','.join(values)}) values ({','.join('?' * len(values))})", list(values.values()))
    con.commit()
    con.close()
    return path


def _env(path: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "AATM_NEW_PASSWORD"}
    env.update(DATABASE_URL=f"sqlite+aiosqlite:///{path}", SYNC_DATABASE_URL=f"sqlite:///{path}",
               DEFAULT_ADMIN_EMAIL=EMAIL, AATM_RESET_ALLOW_OTHER_DB="1",
               AATM_RESET_LOG=str(tmp_path / "reset.log"), **extra)
    return env


def _run(db: Path, *args: str, stdin: str = "", **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(SCRIPT), "--no-force-change", *args], input=stdin, text=True,
                          capture_output=True, env=_env(db, db.parent, **extra), timeout=90, check=False)


def _row(db: Path) -> tuple[str, int]:
    con = sqlite3.connect(db)
    try:
        return con.execute("select password_hash, token_version from users where email=?", (EMAIL,)).fetchone()
    finally:
        con.close()


def test_prompt_changes_password_and_reports_success(db: Path) -> None:
    result = _run(db, "--prompt", stdin=f"{PASSWORD}\n{PASSWORD}\n")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "[AATM Reset] SUCCESS" in result.stdout.strip().splitlines()[-1]
    assert PASSWORD not in output
    stored, version = _row(db)
    assert version == 3 and verify_password(PASSWORD, stored)
    log = (db.parent / "reset.log").read_text()
    assert "SUCCESS" in log and PASSWORD not in log and stored not in log


def test_mismatch_can_be_retried_then_fails_clearly(db: Path) -> None:
    result = _run(db, "--prompt", stdin="aaaaaaaaaaaaaa\nbbbbbbbbbbbbbb\n" * 3)
    assert result.returncode == 1
    assert "FAILED" in result.stderr.strip().splitlines()[-1]
    assert _row(db)[1] == 2


def test_short_password_changes_nothing(db: Path) -> None:
    result = _run(db, AATM_NEW_PASSWORD="short")
    assert result.returncode == 1 and "at least 12" in result.stderr
    assert _row(db)[1] == 2


def test_waits_for_a_brief_writer(db: Path) -> None:
    def hold() -> None:
        con = sqlite3.connect(db, isolation_level=None)
        con.execute("begin immediate")
        ready.set()
        time.sleep(7)
        con.execute("rollback")
        con.close()

    ready = threading.Event()
    thread = threading.Thread(target=hold)
    thread.start()
    ready.wait(5)
    result = _run(db, AATM_NEW_PASSWORD=PASSWORD)
    thread.join()
    assert result.returncode == 0, result.stdout + result.stderr
    assert _row(db)[1] == 3


def test_locked_database_is_a_clear_failure_without_secrets(db: Path) -> None:
    con = sqlite3.connect(db, isolation_level=None)
    con.execute("begin immediate")
    try:
        result = _run(db, AATM_NEW_PASSWORD=PASSWORD, AATM_RESET_BUSY_SECONDS="0.2")
    finally:
        con.execute("rollback")
        con.close()
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "FAILED: the database was not updated" in result.stderr
    assert "Traceback" not in output and "pbkdf2" not in output and PASSWORD not in output
    assert _row(db)[1] == 2
