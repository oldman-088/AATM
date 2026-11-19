"""Database backup taken before destructive admin actions (bulk cancel, chat clear, replacement).

Uses the SQLite online backup API, so the copy is consistent even while the app is writing.
Files are written to ``data/backups/`` with mode 0600 and only the newest ``BACKUP_KEEP`` are kept.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings

BACKUP_KEEP = 20
_PREFIX = "aatm-"
_SUFFIX = ".db"


def _default_source() -> Path:
    url = get_settings().get_sync_database_url()
    for scheme in ("sqlite+pysqlite:///", "sqlite:///"):
        if url.startswith(scheme):
            return Path(url[len(scheme) :])
    raise RuntimeError("database backup supports only SQLite; take a server-side dump for other databases")


def _default_backups_dir(source: Path) -> Path:
    # Next to the database: data/backups/ for the app's own data/aatm.db.
    return source.parent / "backups"


def _safe_reason(reason: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-")
    return slug[:40] or "manual"


def _prune(backups_dir: Path, keep: int) -> None:
    files = sorted(p for p in backups_dir.iterdir() if p.name.startswith(_PREFIX) and p.suffix == _SUFFIX)
    for old in files[: max(0, len(files) - keep)]:
        old.unlink(missing_ok=True)


def backup_database(
    reason: str,
    *,
    source_path: Path | None = None,
    backups_dir: Path | None = None,
    keep: int = BACKUP_KEEP,
) -> Path:
    """Copy the SQLite database to ``data/backups/`` and return the backup path."""
    src = Path(source_path) if source_path is not None else _default_source()
    if not src.is_file():
        raise FileNotFoundError(f"database not found: {src}")
    out_dir = Path(backups_dir) if backups_dir is not None else _default_backups_dir(src)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = out_dir / f"{_PREFIX}{stamp}-{_safe_reason(reason)}{_SUFFIX}"
    n = 1
    while dest.exists():
        dest = out_dir / f"{_PREFIX}{stamp}-{n}-{_safe_reason(reason)}{_SUFFIX}"
        n += 1

    # Create the file with 0600 before any data is written into it.
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    os.chmod(dest, 0o600)

    _prune(out_dir, keep)
    return dest


async def backup_before(reason: str) -> str:
    """Back up before a destructive admin action; refuse the action (503) when no backup can be taken."""
    import asyncio

    from fastapi import HTTPException

    try:
        path = await asyncio.to_thread(backup_database, reason)
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "backup_failed", "message": f"No backup could be taken, so nothing was changed: {type(exc).__name__}"},
        ) from exc
    return str(path)
