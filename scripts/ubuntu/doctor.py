#!/usr/bin/env python3
"""AATM doctor — find state that has drifted out of sync (E17). Report-only unless ``--fix`` is given.

This module is the *library* of state checks. Run it through ``aatm_doctor.py``, which keeps this
file free of duplicates and adds the two window-level checks that state alone cannot show:
``window_on_wrong_repo`` (E34) and ``account_terms_not_accepted`` (E25).

Checks (each finding names the row it is about):
  worker_working_without_task     a worker is "working" but no task is running for it
  window_connected_worker_offline a worker is "offline" while its Arena window is connected (bridge)
  session_open_for_finished_task  an Arena session is still open for a task that already finished
  task_assigned_to_manager        an implementation task is assigned to manager-0 (it cannot run there)
  queued_task_stuck               a task whose dependencies are all done has been queued for over 15 minutes
  window_script_outdated          an Arena window runs an older connector script than this checkout
  manager_job_stuck_needs_human   a manager job has waited in needs_human for over 15 minutes

``--fix`` repairs only the safe cases, and only after a database backup (data/backups/):
  - worker_working_without_task    -> idle (only when no Arena session is open for that worker)
  - window_connected_worker_offline -> idle (only when no task is running for that worker)
  - session_open_for_finished_task -> closed
Every other finding is reported for a person to decide.

The database is opened read-only for reports. Standard library only, so it runs with any Python 3.
``scripts/ubuntu/aatm_doctor.py`` runs this module's checks, adds the window-level ones, and is what
the watchdog calls every 10 minutes and what ``start-all.sh`` runs once after the stack is ready.
The doctor itself never restarts anything.

Usage:
  scripts/ubuntu/doctor.py                 # report (exit 0: clean, 1: findings, 2: cannot read the database)
  scripts/ubuntu/doctor.py --fix           # back up, then repair the safe cases, then report what remains
  scripts/ubuntu/doctor.py --json          # machine-readable report
  scripts/ubuntu/aatm doctor              # aatm_doctor.py: these checks plus the window-level ones
  scripts/ubuntu/aatm doctor-state [--fix] # this module on its own, with no window checks
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
QUEUED_STUCK_MINUTES = 15
NEEDS_HUMAN_STUCK_MINUTES = 15
HEARTBEAT_GRACE_MINUTES = 5  # a worker that just changed state is not "working without a task" yet
MANAGER_AGENT_ID = "manager-0"
DONE_TASK_STATUSES = ("completed", "done", "merged")
FINISHED_TASK_STATUSES = ("completed", "done", "merged", "cancelled", "failed")
TERMINAL_TASK_STATUSES = FINISHED_TASK_STATUSES
ARENA_CONNECTOR_SOURCE = ROOT_DIR / "backend" / "app" / "browser" / "arena_connector.py"


class BackupRefused(OSError):
    """A safe fix was asked for but no backup could be written, so nothing was changed.

    It is an ``OSError`` because that is what a failed copy already was: callers that treated a
    missing backup as an I/O problem (and ``backend/tests/test_e17_doctor.py`` does) keep working.
    """


@dataclass
class Finding:
    check: str
    subject: str
    message: str
    fixable: bool = False
    fix: str | None = None  # what --fix does, when fixable

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------- helpers
def parse_ts(value: Any) -> datetime | None:
    """SQLite stores naive UTC timestamps; a value without a zone is read as UTC."""
    if value is None or value == "":
        return None
    text = str(value).strip().replace("T", " ")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def version_tuple(value: str | None) -> tuple[int, ...] | None:
    match = re.match(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", value or "")
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def current_script_version() -> str | None:
    """The connector script version this checkout ships (read from the source; the doctor never imports the app)."""
    try:
        text = ARENA_CONNECTOR_SOURCE.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^SCRIPT_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]


def _marks(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


# --------------------------------------------------------------------------------------- checks
def check_workers_working_without_task(conn, now: datetime) -> list[Finding]:
    cutoff = now - timedelta(minutes=HEARTBEAT_GRACE_MINUTES)
    findings: list[Finding] = []
    agents = _rows(conn, "SELECT id, status, current_task_id, last_heartbeat FROM agents "
                         "WHERE role IN ('worker','reviewer') AND status = 'working'")
    for agent in agents:
        heartbeat = parse_ts(agent["last_heartbeat"])
        if heartbeat is not None and heartbeat > cutoff:
            continue
        running = _rows(conn, "SELECT id FROM tasks WHERE status = 'in_progress' AND "
                              "(assigned_agent_id = ? OR id = ?)", (agent["id"], agent["current_task_id"]))
        if running:
            continue
        open_sessions = _rows(conn, "SELECT id FROM arena_sessions WHERE agent_id = ? AND is_open = 1", (agent["id"],))
        fixable = not open_sessions
        findings.append(Finding(
            "worker_working_without_task", agent["id"],
            f"{agent['id']} is 'working' but no task is running for it"
            + (f" (its current task {agent['current_task_id']} is not in progress)" if agent["current_task_id"] else "")
            + (f"; an Arena session is still open ({len(open_sessions)})" if open_sessions else "") + ".",
            fixable=fixable,
            fix="set the worker to idle and clear its current task" if fixable else None,
        ))
    return findings


def check_window_connected_worker_offline(conn, bridge: dict[str, dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    for agent in _rows(conn, "SELECT id, status, browser_profile_id, current_task_id FROM agents "
                             "WHERE status = 'offline' AND browser_profile_id IS NOT NULL"):
        window = bridge.get(agent["browser_profile_id"]) or {}
        if not window.get("connected"):
            continue
        running = _rows(conn, "SELECT id FROM tasks WHERE status = 'in_progress' AND "
                              "(assigned_agent_id = ? OR id = ?)", (agent["id"], agent["current_task_id"]))
        fixable = not running
        findings.append(Finding(
            "window_connected_worker_offline", agent["id"],
            f"{agent['id']} is 'offline' but its Arena window ({agent['browser_profile_id']}) is connected.",
            fixable=fixable,
            fix="set the worker to idle (its window is connected)" if fixable else None,
        ))
    return findings


def check_session_open_for_finished_task(conn) -> list[Finding]:
    marks = _marks(FINISHED_TASK_STATUSES)
    rows = _rows(conn, f"SELECT s.id AS session_id, s.phase, t.id AS task_id, t.status AS task_status "
                       f"FROM arena_sessions s JOIN tasks t ON t.id = COALESCE(s.active_task_id, s.task_id) "
                       f"WHERE s.is_open = 1 AND t.status IN ({marks})", FINISHED_TASK_STATUSES)
    return [
        Finding("session_open_for_finished_task", row["session_id"],
                f"Arena session {row['session_id']} is still open for task {row['task_id']} ({row['task_status']}).",
                fixable=True, fix="close the session")
        for row in rows
    ]


def check_tasks_assigned_to_manager(conn) -> list[Finding]:
    marks = _marks(TERMINAL_TASK_STATUSES)
    rows = _rows(conn, f"SELECT id, title, status FROM tasks WHERE assigned_agent_id = ? AND task_type != 'review' "
                       f"AND status NOT IN ({marks})", (MANAGER_AGENT_ID, *TERMINAL_TASK_STATUSES))
    return [
        Finding("task_assigned_to_manager", row["id"],
                f"Task '{row['title']}' ({row['status']}) is assigned to {MANAGER_AGENT_ID}; a worker must own it.")
        for row in rows
    ]


def check_queued_tasks_stuck(conn, now: datetime) -> list[Finding]:
    cutoff = now - timedelta(minutes=QUEUED_STUCK_MINUTES)
    statuses = {r["id"]: r["status"] for r in _rows(conn, "SELECT id, status FROM tasks")}
    findings: list[Finding] = []
    for row in _rows(conn, "SELECT id, title, dependencies, updated_at FROM tasks WHERE status = 'queued'"):
        queued_since = parse_ts(row["updated_at"])
        if queued_since is None or queued_since > cutoff:
            continue
        try:
            deps = json.loads(row["dependencies"] or "[]")
        except (TypeError, ValueError):
            deps = []
        if not all(statuses.get(dep) in DONE_TASK_STATUSES for dep in deps):
            continue  # waiting on a prerequisite is normal
        findings.append(Finding(
            "queued_task_stuck", row["id"],
            f"Task '{row['title']}' has all its dependencies done but has been queued since {queued_since:%Y-%m-%d %H:%M} UTC.",
        ))
    return findings


def check_window_script_outdated(bridge: dict[str, dict[str, Any]], current: str | None) -> list[Finding]:
    want = version_tuple(current)
    if want is None:
        return []
    findings: list[Finding] = []
    for profile_id, window in sorted(bridge.items()):
        have = version_tuple(window.get("script_version"))
        if have is not None and have < want:
            findings.append(Finding(
                "window_script_outdated", profile_id,
                f"Window {profile_id} runs connector script {window.get('script_version')}; this checkout ships {current}. "
                "Generate the script again and reinstall it in Tampermonkey.",
            ))
    return findings


def check_manager_jobs_needs_human(conn, now: datetime) -> list[Finding]:
    cutoff = now - timedelta(minutes=NEEDS_HUMAN_STUCK_MINUTES)
    findings: list[Finding] = []
    for row in _rows(conn, "SELECT id, kind, updated_at, error FROM manager_jobs WHERE status = 'needs_human'"):
        since = parse_ts(row["updated_at"])
        if since is None or since > cutoff:
            continue
        findings.append(Finding(
            "manager_job_stuck_needs_human", row["id"],
            f"Manager {row['kind']} job {row['id']} has needed a human since {since:%Y-%m-%d %H:%M} UTC"
            + (f": {str(row['error'])[:160]}" if row["error"] else "")
            + ". Resolve it, or cancel it with POST /api/v1/arena/manager/jobs/{id}/cancel.",
        ))
    return findings


def run_checks(conn: sqlite3.Connection, *, now: datetime, bridge: dict[str, dict[str, Any]] | None,
               script_version: str | None,
               extra: Callable[..., list[Finding]] | None = None) -> list[Finding]:
    """Every check. ``bridge`` is None when the bridge is unreachable: the two window checks are then skipped.

    ``extra`` lets a caller (``aatm_doctor.py``) append its own checks to the same read-only connection
    instead of copying this list: it is called as ``extra(conn, bridge)`` and returns findings.
    """
    findings: list[Finding] = []
    findings += check_workers_working_without_task(conn, now)
    if bridge is not None:
        findings += check_window_connected_worker_offline(conn, bridge)
        findings += check_window_script_outdated(bridge, script_version)
    findings += check_session_open_for_finished_task(conn)
    findings += check_tasks_assigned_to_manager(conn)
    findings += check_queued_tasks_stuck(conn, now)
    findings += check_manager_jobs_needs_human(conn, now)
    if extra is not None:
        findings += extra(conn, bridge)
    return findings


# --------------------------------------------------------------------------------------- fixes
def backup_before_fix(db_path: Path, reason: str) -> Path:
    """Back up with the app's helper (SQLite online backup, 0600, newest 20 kept); the fix is refused without it."""
    backend = str(ROOT_DIR / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.db.backup import backup_database

    return backup_database(reason, source_path=db_path)


def apply_safe_fixes(db_path: Path, findings: list[Finding], *, now: datetime,
                     backup_fn: Callable[[Path, str], Path] | None = None) -> list[dict[str, Any]]:
    """Repair only the fixable findings, inside one transaction, after a backup. Returns what was changed.

    ``backup_fn`` defaults to :func:`backup_before_fix`; a caller that must keep working without the
    backend's venv (``aatm_doctor.py``) passes its own, but no finding is ever touched before it returns.
    """
    fixable = [f for f in findings if f.fixable]
    if not fixable:
        return []
    try:
        backup = (backup_fn or backup_before_fix)(db_path, "doctor fix")
    except Exception as exc:  # a fix without a copy to go back to is not a safe fix
        raise BackupRefused(f"no backup could be written ({type(exc).__name__}: {exc}); nothing was changed") from exc
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    changed: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        for finding in fixable:
            if finding.check in {"worker_working_without_task", "window_connected_worker_offline"}:
                conn.execute("UPDATE agents SET status = 'idle', current_task_id = NULL WHERE id = ?", (finding.subject,))
            elif finding.check == "session_open_for_finished_task":
                conn.execute("UPDATE arena_sessions SET is_open = 0, phase = 'closed', closed_at = ? WHERE id = ?",
                             (stamp, finding.subject))
            else:
                continue
            changed.append({"check": finding.check, "subject": finding.subject, "fix": finding.fix})
        conn.commit()
    finally:
        conn.close()
    for item in changed:
        item["backup_path"] = str(backup)
    return changed


# --------------------------------------------------------------------------------------- bridge / config
def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return values


def database_path(env: dict[str, str]) -> Path:
    url = os.environ.get("DATABASE_URL") or env.get("DATABASE_URL") or ""
    match = re.match(r"sqlite(?:\+\w+)?:///(.+)$", url)
    if not match:
        return ROOT_DIR / "data" / "aatm.db"
    raw = Path(match.group(1))
    if raw.is_absolute():
        return raw
    for base in (ROOT_DIR, ROOT_DIR / "backend"):
        if (base / raw).exists():
            return (base / raw).resolve()
    return (ROOT_DIR / raw).resolve()


def fetch_bridge_windows(env: dict[str, str], timeout: float = 4.0) -> dict[str, dict[str, Any]] | None:
    """Read-only: each Arena window's connection and script version. None when the bridge cannot be asked."""
    port = os.environ.get("ARENA_BRIDGE_PORT") or env.get("ARENA_BRIDGE_PORT") or "4100"
    token = (os.environ.get("ARENA_BRIDGE_TOKEN") or env.get("ARENA_BRIDGE_TOKEN") or "").strip()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/connector/profiles")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return {p["profile_id"]: p for p in data.get("profiles", []) if isinstance(p, dict) and p.get("profile_id")}


# --------------------------------------------------------------------------------------- main
def diagnose(db_path: Path, *, now: datetime, bridge: dict[str, dict[str, Any]] | None,
             script_version: str | None, fix: bool,
             extra: Callable[..., list[Finding]] | None = None,
             backup_fn: Callable[[Path, str], Path] | None = None) -> dict[str, Any]:
    """Read-only report; with ``fix`` the safe findings are repaired (after a backup) and re-checked.

    ``extra`` / ``backup_fn`` are how ``aatm_doctor.py`` adds its checks and its own backup strategy
    without duplicating anything: the checks and the fix SQL stay in this module.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        findings = run_checks(conn, now=now, bridge=bridge, script_version=script_version, extra=extra)
    finally:
        conn.close()
    fixed: list[dict[str, Any]] = []
    if fix:
        fixed = apply_safe_fixes(db_path, findings, now=now, backup_fn=backup_fn)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            findings = run_checks(conn, now=now, bridge=bridge, script_version=script_version, extra=extra)
        finally:
            conn.close()
    return {
        "checked_at": now.isoformat(),
        "database": str(db_path),
        "bridge_checked": bridge is not None,
        "fixed": fixed,
        "findings": [f.as_dict() for f in findings],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--fix", action="store_true", help="back up, then repair the safe cases")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--once", action="store_true", help="accepted for the watchdog (the doctor always runs once)")
    parser.add_argument("--db", type=Path, help="database file (default: DATABASE_URL from .env, else data/aatm.db)")
    parser.add_argument("--no-bridge", action="store_true", help="skip the window checks (no bridge call)")
    args = parser.parse_args(argv)

    env = read_env(ROOT_DIR / ".env")
    db_path = args.db or database_path(env)
    if not db_path.exists():
        print(f"doctor: database not found: {db_path}", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    bridge = None if args.no_bridge else fetch_bridge_windows(env)
    try:
        report = diagnose(db_path, now=now, bridge=bridge, script_version=current_script_version(), fix=args.fix)
    except BackupRefused as exc:
        print(f"doctor: refusing to fix: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"doctor: cannot read the database: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"AATM doctor — {report['checked_at']} — database {report['database']}")
        if not report["bridge_checked"]:
            print("  note: the bridge did not answer (or --no-bridge was given); window checks were skipped.")
        for item in report["fixed"]:
            print(f"  FIXED  {item['check']} {item['subject']}: {item['fix']} (backup {item['backup_path']})")
        for item in report["findings"]:
            tag = "FIXABLE" if item["fixable"] else "REPORT "
            print(f"  {tag} {item['check']} {item['subject']}: {item['message']}")
        if not report["findings"]:
            print("  OK: no problems found.")
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
