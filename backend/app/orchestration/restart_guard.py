"""Safe restart: no new review starts while a backend restart waits (owner, 2026-11-26).

Twice on 2026-11-26 a backend restart cut a review off in the middle: the manager ran PR #9's review again
from the start, and a worker stayed bound to a peer review that no longer ran. ``scripts/ubuntu/safe_restart.py``
now writes ``data/restart.pending`` and waits until no manager job runs before it restarts the backend. While
the file is there the manager and the peer reviewers take no new job; the ones already running finish.

The flag cannot pause the team for long: it is ignored once it is older than ``MAX_PENDING_SECONDS`` (the
script gives up well before that), and the backend removes it when it starts.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.core.config import get_settings

FLAG_NAME = "restart.pending"
MAX_PENDING_SECONDS = 45 * 60


def flag_path() -> Path:
    return Path(get_settings().DATA_DIR) / FLAG_NAME


def restart_pending(now: float | None = None) -> bool:
    path = flag_path()
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age < MAX_PENDING_SECONDS


def clear_restart_flag() -> None:
    try:
        flag_path().unlink()
    except OSError:
        pass
