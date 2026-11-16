"""P1 — installer & startup fixes that must not regress.

* relative SQLite URLs resolve against the repository root (not the caller's cwd)
* install.sh supports `--retry-downloads` and pins free ports for the docker services
* start.sh validates the backend with a health check instead of trusting a PID file
* restart.sh / reset-admin-password.sh exist, are valid bash and the reset really works
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from app.core.config import (
    ROOT_DIR,
    normalize_async_database_url,
    normalize_sync_database_url,
    resolve_sqlite_url_path,
)
from app.core.security import verify_password

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "ubuntu"


def test_relative_sqlite_urls_resolve_against_repo_root_not_cwd(tmp_path, monkeypatch):
    async_url = normalize_async_database_url("sqlite+aiosqlite:///./data/aatm.db")
    sync_url = normalize_sync_database_url("sqlite:///./data/aatm.db")
    assert async_url == f"sqlite+aiosqlite:///{(ROOT_DIR / 'data' / 'aatm.db').as_posix()}"
    assert sync_url == f"sqlite:///{(ROOT_DIR / 'data' / 'aatm.db').as_posix()}"

    # Running Alembic from backend/ used to create backend/data/aatm.db (or fail to open it).
    monkeypatch.chdir(REPO_ROOT / "backend")
    assert normalize_sync_database_url("sqlite:///./data/aatm.db") == sync_url

    nested = f"sqlite:///{tmp_path / 'nested' / 'deep' / 'aatm.db'}"
    assert resolve_sqlite_url_path(nested).startswith("sqlite:///")
    assert (tmp_path / "nested" / "deep").is_dir()


def test_absolute_and_special_database_urls_are_left_alone(tmp_path):
    absolute = f"sqlite+aiosqlite:///{(tmp_path / 'x.db').as_posix()}"
    assert normalize_async_database_url(absolute) == absolute
    assert normalize_async_database_url("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"
    assert normalize_sync_database_url("sqlite:///:memory:") == "sqlite:///:memory:"
    pg = "postgresql://" + "aatm:pw" + "@127.0.0.1:5432/aatm"
    assert normalize_sync_database_url(pg).startswith("postgresql+psycopg://")
    assert normalize_async_database_url(pg) == normalize_sync_database_url(pg)


def test_install_start_and_reset_scripts_are_valid_bash():
    for name in ("install.sh", "start.sh", "restart.sh", "reset-admin-password.sh", "verify.sh", "stop.sh"):
        path = SCRIPTS / name
        assert path.is_file(), name
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_installer_picks_free_ports_retries_downloads_and_keeps_secrets():
    text = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    assert "--retry-downloads" in text
    assert "with_retries" in text
    for key in ("POSTGRES_PORT", "RABBITMQ_PORT", "RABBITMQ_MANAGEMENT_PORT", "REDIS_PORT"):
        assert key in text, key
    assert "is_free(" in text and "ephemeral_port(" in text
    # Re-running must not regenerate secrets or the admin password.
    assert "keeping existing secrets" in text
    assert 'if [[ ! -f .env ]]; then' in text
    assert "Existing admin kept" in text
    # The SQLite fallback is pinned to an absolute path under the repository root.
    assert 'db_file = data / "aatm.db"' in text


def test_start_script_does_not_trust_a_stale_pid_file():
    text = (SCRIPTS / "start.sh").read_text(encoding="utf-8")
    assert "backend_healthy()" in text
    assert "/api/health" in text
    assert "replacing it" in text
    restart = (SCRIPTS / "restart.sh").read_text(encoding="utf-8")
    assert "start.sh" in restart and "stop_service backend" in restart


def test_reset_admin_password_script_rotates_the_hash_and_tokens(tmp_path):
    db_path = tmp_path / "reset-admin.sqlite"
    sync_url = f"sqlite:///{db_path.as_posix()}"
    async_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env = {
        **os.environ,
        "SYNC_DATABASE_URL": sync_url,
        "DATABASE_URL": async_url,
        "AATM_SANDBOX_MODE": "process",
        "AATM_ALLOW_PROCESS_SANDBOX": "true",
        "ENVIRONMENT": "development",
        "SECRET_KEY": "a-test-secret-key-with-at-least-32-bytes",
        "AATM_NEW_PASSWORD": "brand-new-admin-password-2026",
        "AATM_ADMIN_EMAIL": "admin@aatm.local",
    }
    # First run creates + migrates the database and the admin account.
    first = subprocess.run(
        ["bash", str(SCRIPTS / "reset-admin-password.sh")], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT)
    )
    assert first.returncode == 0, first.stderr + first.stdout
    assert "brand-new-admin-password-2026" in first.stdout

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT password_hash, must_change_password, token_version FROM users WHERE email = 'admin@aatm.local'"
        ).fetchone()
    assert row is not None
    assert verify_password("brand-new-admin-password-2026", row[0])
    assert int(row[1]) == 1  # forced change at next login
    version_after_first = int(row[2])

    # Second run bumps token_version again so previously issued JWTs stop working.
    env["AATM_NEW_PASSWORD"] = "second-new-admin-password-2026"
    second = subprocess.run(
        ["bash", str(SCRIPTS / "reset-admin-password.sh")], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT)
    )
    assert second.returncode == 0, second.stderr + second.stdout
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT password_hash, token_version FROM users WHERE email = 'admin@aatm.local'"
        ).fetchone()
    assert verify_password("second-new-admin-password-2026", row[0])
    assert int(row[1]) == version_after_first + 1
