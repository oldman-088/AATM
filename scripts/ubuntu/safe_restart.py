#!/usr/bin/env python3
"""AATM safe backend restart: wait until no manager or peer review runs, then restart (owner, 2026-11-26).

Twice on 2026-11-26 a restart cut a review off in the middle (the manager reviewed PR #9 again from the
start; a worker stayed bound to a dead peer review). This script:

1. writes ``data/restart.pending``, so the manager and the peer reviewers take no new job
   (``backend/app/orchestration/restart_guard.py``);
2. waits until no manager job is ``running`` in the database (the running ones finish normally);
3. restarts only the backend (``restart.sh --keep-bridge --keep-frontend``); the new backend removes the flag.

If the jobs do not finish within ``--timeout`` seconds, nothing is restarted, the flag is removed and the
exit code is 3, so work goes on as before. ``--now`` restarts without waiting (the old behaviour).

Usage: scripts/ubuntu/safe_restart.py [--timeout 1500] [--poll 10] [--now] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "aatm.db"
FLAG_PATH = ROOT / "data" / "restart.pending"
EXIT_TIMEOUT = 3


def running_jobs(db_path: Path) -> list[str]:
    """Ids and kinds of manager jobs (manager or peer) that are running now."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        return [f"{kind}:{job_id[:8]}" for job_id, kind in
                conn.execute("SELECT id, kind FROM manager_jobs WHERE status = 'running'")]
    except sqlite3.OperationalError:
        return []  # no manager_jobs table: nothing can be cut off
    finally:
        conn.close()


def wait_until_quiet(db_path: Path, flag_path: Path, *, timeout: float, poll: float,
                     clock=time.monotonic, sleep=time.sleep, log=print) -> bool:
    """Raise the flag and wait for the running jobs. True when quiet; False (flag removed) on timeout."""
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(f"{time.time():.0f}\n", encoding="utf-8")
    deadline = clock() + timeout
    last: list[str] | None = None
    while True:
        busy = running_jobs(db_path)
        if not busy:
            return True
        if busy != last:
            log(f"[AATM Safe Restart] waiting for {len(busy)} running job(s): {', '.join(busy)}")
            last = busy
        if clock() >= deadline:
            flag_path.unlink(missing_ok=True)
            log(f"[AATM Safe Restart] still running after {timeout:.0f}s: not restarting; work goes on.")
            return False
        flag_path.touch()  # keep the flag fresh while waiting
        sleep(poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=1500.0)
    parser.add_argument("--poll", type=float, default=10.0)
    parser.add_argument("--now", action="store_true", help="restart without waiting for running reviews")
    parser.add_argument("--dry-run", action="store_true", help="wait, but do not restart (the flag is removed)")
    args = parser.parse_args(argv)
    if not args.now and not wait_until_quiet(DB_PATH, FLAG_PATH, timeout=args.timeout, poll=args.poll):
        return EXIT_TIMEOUT
    if args.dry_run:
        FLAG_PATH.unlink(missing_ok=True)
        print("[AATM Safe Restart] quiet now (dry run: not restarting).")
        return 0
    print("[AATM Safe Restart] no review is running: restarting the backend.")
    result = subprocess.run([str(ROOT / "scripts/ubuntu/restart.sh"), "--keep-bridge", "--keep-frontend"], check=False)
    if result.returncode != 0:
        FLAG_PATH.unlink(missing_ok=True)  # a failed restart must not leave reviews paused
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
