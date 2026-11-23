#!/usr/bin/env python3
"""aatm doctor — the one entry point for AATM's autonomy checks (E17, with E25 and E34).

It runs every check in ``doctor.py`` on one read-only connection and adds the two that a window
needs and the state tables alone cannot show. Nothing here re-implements a check: ``doctor.py``
owns the state checks and the fix SQL, this file owns the wiring, the window-level checks and the
report shape.

The four checks the owner is told to expect, in one place:
  worker_working_without_task      a worker is "working" with no task running for it   (doctor.py)
  window_connected_worker_offline  a worker is "offline" while its window is connected  (doctor.py)
  session_open_for_finished_task   an open Arena session for a task that already ended  (doctor.py)
  window_on_wrong_repo             a window sits on another repository than its task's (this file, E34)

Plus the alarm the owner asked for: ``account_terms_not_accepted`` (E25) lists the accounts whose
Arena page is stopped at the Terms of Service, so they are cleared once before any task runs.
AATM never accepts terms for a human, so that finding is never fixable.

Safety policy (the whole point of this tool):
  * without ``--fix`` nothing is written at all: the database is opened ``mode=ro`` and the bridge
    is only read;
  * with ``--fix`` only findings marked ``fixable`` are repaired, each fix is preceded by a backup,
    and the fix is refused when the backup fails;
  * anything needing a human decision — Terms of Service, CAPTCHA, login, a wrong repository in a
    live window, a task to requeue — is reported and never touched. Selecting a repository clicks
    in somebody's browser, so ``window_on_wrong_repo`` is report-only by design.

Run it by hand, after every start, and every 10 minutes from the watchdog:
  scripts/ubuntu/aatm doctor                      # report; exit 0 clean, 1 findings, 2 unreadable DB
  scripts/ubuntu/aatm doctor --fix                # back up first, repair the safe cases, report the rest
  scripts/ubuntu/aatm_doctor.py --json            # machine-readable report (what the watchdog reads)
  scripts/ubuntu/aatm_doctor.py --json --bridge-file windows.json   # same, with windows from a file
  scripts/ubuntu/aatm_doctor.py --json --log      # also append one line to logs/doctor.log
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
BACKUP_KEEP = 20


def _log_file() -> Path:
    """Where the one-line summary is appended (AATM_DOCTOR_LOG overrides; tests and CI point it at a tmp dir)."""
    return Path(os.environ.get("AATM_DOCTOR_LOG") or ROOT_DIR / "logs" / "doctor.log")


def _backup_dir() -> Path:
    """Where a safe fix writes its copy (AATM_DOCTOR_BACKUP_DIR overrides; it must exist before any fix)."""
    return Path(os.environ.get("AATM_DOCTOR_BACKUP_DIR") or ROOT_DIR / "data" / "backups")

#: The four checks the owner's acceptance criteria name. ``--json`` lists them in ``required_checks``
#: and in ``checks_run``, so a report alone proves all four ran.
REQUIRED_CHECKS = (
    "worker_working_without_task",
    "window_connected_worker_offline",
    "session_open_for_finished_task",
    "window_on_wrong_repo",
)

#: Checks that live in ``doctor.py`` (this file must not grow a second copy of any of them).
STATE_CHECKS = (
    "worker_working_without_task",
    "window_connected_worker_offline",
    "session_open_for_finished_task",
    "task_assigned_to_manager",
    "queued_task_stuck",
    "window_script_outdated",
    "manager_job_stuck_needs_human",
)

#: Window-level checks added here.
WINDOW_CHECKS = ("window_on_wrong_repo", "account_terms_not_accepted", "account_human_check_pending")

#: Codes that mean "this chat is not provably on the task's repository" (E34). The same set the
#: runner treats as a reason not to submit; kept here because the doctor never imports the app.
REPO_SAFETY_CODES = (
    "repo_mismatch",
    "repo_unverified",
    "repo_not_selected",
    "repo_unknown",
    "repo_not_available",
    "repo_control_not_found",
    "invalid_repo",
)

#: What the userscript quotes when the selected repository is wrong: "selected repository is X, the
#: task needs Y" (backend/app/browser/arena_connector.py, cmdSubmit).
SELECTED_REPO_RE = re.compile(r"selected repository is\s+(?P<have>\S+?)[,.]?\s+the task needs\s+(?P<want>\S+)", re.I)
OWNER_NAME_RE = re.compile(r"[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")

#: Human-check phrases, from the bridge's own stop list (bridge/connector.py). Only the *category*
#: matters here: every one of these stays with the owner, none of them is ever automated.
HUMAN_CHECK_CATEGORIES: dict[str, tuple[str, ...]] = {
    "terms": ("terms of service", "terms of use", "accept the terms", "agree to the terms", "accepter les conditions"),
    "captcha": ("captcha", "turnstile", "hcaptcha", "recaptcha", "verify you are human", "prove you are human",
                "prove you're human", "complete the check", "challenges.cloudflare.com", "unusual traffic"),
    "login": ("sign in", "log in", "login", "signin", "authenticate", "session expired", "not authorized"),
}
#: An unread Terms page is a setup step, not a failure: it is the one category the report calls out
#: by name, because clearing it once unblocks the account for every later task (E25).
TERMS_CATEGORY = "terms"

OWNER_HINT_TERMS = (
    "Open that account's Arena window and accept the Terms of Service yourself; AATM never accepts "
    "them for a human (E25). Once accepted, resume the profile from the dashboard or POST "
    "/api/v1/arena/profiles/{profile_id}/reset."
)
OWNER_HINT_REPO = (
    "Select the task's repository in that window (or let the dispatcher run select_repo before it "
    "submits, E34). Nothing is changed from here: clicking the repo control of a live chat could "
    "send the task's prompt to the wrong repository."
)


def _load(module_name: str, filename: str) -> Any:
    """Import a sibling script by path: scripts/ubuntu/ is a folder of stand-alone scripts, not a package."""
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - only if the file vanished
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


doctor = _load("aatm_doctor_base", "doctor.py")


# --------------------------------------------------------------------------------- shared helpers
def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """doctor._rows, but a missing table is treated as "nothing to report" instead of a crash."""
    try:
        return doctor._rows(conn, sql, params)
    except sqlite3.Error:
        return []


def normalize_repo(value: str | None) -> str | None:
    """``owner/name`` from a git URL, an ssh remote, or a bare ``owner/name``; None when unparseable."""
    text = (value or "").strip().rstrip("/")
    if not text:
        return None
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
        return text
    match = OWNER_NAME_RE.search(text)
    return match.group(1) if match else None


def human_check_category(reason: str | None) -> str | None:
    """Which human check this page is stopped at: ``terms``, ``captcha``, ``login`` — or None."""
    text = (reason or "").lower()
    if not text:
        return None
    for category, phrases in HUMAN_CHECK_CATEGORIES.items():
        if any(phrase in text for phrase in phrases):
            return category
    return None


# --------------------------------------------------------------------------------------- E34 check
def _project_repos(conn: sqlite3.Connection) -> dict[str, str]:
    """project id -> ``owner/name``, for the projects whose repository is recorded."""
    repos: dict[str, str] = {}
    for row in _rows(conn, "SELECT id, repo_url, repo_path FROM projects"):
        repo = normalize_repo(row.get("repo_url")) or normalize_repo(row.get("repo_path"))
        if repo:
            repos[row["id"]] = repo
    return repos


def _window_signals(conn: sqlite3.Connection, bridge: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    """One record per window that has an open session or a recorded reason, and where it came from.

    Shared by the two window checks so neither walks the tables twice. ``reasons`` is ordered from
    the freshest signal (the live bridge) to the ones the backend left in the database; each entry is
    prefixed with its source, so a finding can say where it was read.
    """
    repos = _project_repos(conn)
    windows: dict[str, dict[str, Any]] = {}

    def entry(profile_id: str) -> dict[str, Any]:
        return windows.setdefault(str(profile_id), {
            "profile_id": str(profile_id), "name": None, "agent_ids": set(), "session_ids": set(),
            "task_ids": set(), "reasons": [], "project_repos": set(), "connected": None,
            "status": None, "agent_state": None,
        })

    for agent in _rows(conn, "SELECT id, browser_profile_id FROM agents WHERE role IN ('worker','reviewer')"):
        if agent.get("browser_profile_id"):
            entry(agent["browser_profile_id"])["agent_ids"].add(agent["id"])

    for profile in _rows(conn, "SELECT id, name, needs_human_reason FROM browser_profiles"):
        row = entry(profile["id"])
        row["name"] = profile.get("name")
        if profile.get("needs_human_reason"):
            row["reasons"].append(f"browser_profiles.needs_human_reason: {profile['needs_human_reason']}")

    for session in _rows(
        conn,
        "SELECT s.id AS session_id, s.agent_id, s.profile_id, s.project_id, s.error_message, s.needs_human_reason,"
        " t.id AS task_id, t.title AS task_title"
        " FROM arena_sessions s"
        " LEFT JOIN tasks t ON t.id = COALESCE(s.active_task_id, s.task_id)"
        " WHERE s.is_open = 1",
    ):
        if not session.get("profile_id"):
            continue
        row = entry(session["profile_id"])
        row["session_ids"].add(session["session_id"])
        if session.get("agent_id"):
            row["agent_ids"].add(session["agent_id"])
        if session.get("task_id"):
            row["task_ids"].add(f"{session['task_id']} ({session.get('task_title') or '?'})")
        if session.get("project_id") and session["project_id"] in repos:
            row["project_repos"].add(repos[session["project_id"]])
        for column in ("needs_human_reason", "error_message"):
            if session.get(column):
                row["reasons"].append(f"arena_sessions.{column}: {session[column]}")

    for profile_id, window in (bridge or {}).items():
        row = entry(profile_id)
        row["connected"] = bool(window.get("connected"))
        row["agent_state"] = window.get("agent_state")
        row["status"] = window.get("status")
        for column in ("needs_human_reason", "last_error"):
            if window.get(column):
                row["reasons"].insert(0, f"bridge.{column}: {window[column]}")

    return [row for row in windows.values() if row["session_ids"] or row["reasons"]]


def check_windows_on_wrong_repo(conn: sqlite3.Connection,
                                bridge: dict[str, dict[str, Any]] | None = None) -> list[doctor.Finding]:
    """E34 — a window's chat is on another repository than its task's, or cannot prove which one.

    Detected from the codes the connector refuses to submit under (``repo_mismatch`` and friends),
    which the runner records on the session and the profile. Report-only: the fix is a click in a
    live browser, and a wrong click sends the task's prompt into the wrong repository.
    """
    findings: list[doctor.Finding] = []
    for row in _window_signals(conn, bridge):
        hits = [text for text in row["reasons"] if any(code in text.lower() for code in REPO_SAFETY_CODES)]
        if not hits:
            continue
        code = next((c for c in REPO_SAFETY_CODES if any(c in h.lower() for h in hits)), "repo_mismatch")
        have = want = None
        for text in hits:
            match = SELECTED_REPO_RE.search(text)
            if match:
                have, want = match.group("have"), normalize_repo(match.group("want")) or match.group("want")
                break
        expected = want or (sorted(row["project_repos"])[0] if row["project_repos"] else None)
        owners = ", ".join(sorted(row["agent_ids"])) or "unassigned"
        detail = f"window {row['profile_id']}" + (f" ({row['name']})" if row.get("name") else "")
        where = f" is showing {have}" if have else " cannot prove which repository it is on"
        needs = f", the task needs {expected}" if expected else ""
        stuck = ", and the page is stopped there" if row.get("status") == "needs_human" else ""
        findings.append(doctor.Finding(
            "window_on_wrong_repo", row["profile_id"],
            f"{detail} of {owners}{where}{needs}{stuck} ({code}). {OWNER_HINT_REPO} "
            f"Reported from: {hits[0][:180]}",
            fixable=False, fix=None,
        ))
    return findings


# --------------------------------------------------------------------------------------- E25 check
def check_accounts_pending_human_checks(conn: sqlite3.Connection,
                                        bridge: dict[str, dict[str, Any]] | None = None) -> list[doctor.Finding]:
    """E25 — accounts stopped at a page only a person can pass, Terms of Service above all.

    Report-only for every category: AATM never accepts terms, never solves a CAPTCHA, never signs in.
    Terms findings get their own check name so the owner's report lists accounts to clear once at
    setup; a CAPTCHA or a login wall that appears mid-task is the same kind of stop, under
    ``account_human_check_pending``.
    """
    findings: list[doctor.Finding] = []
    for row in _window_signals(conn, bridge):
        categories: dict[str, str] = {}
        for text in row["reasons"]:
            category = human_check_category(text)
            if category and category not in categories:
                categories[category] = text
        if not categories:
            continue
        owners = ", ".join(sorted(row["agent_ids"])) or "unassigned"
        detail = f"window {row['profile_id']}" + (f" ({row['name']})" if row.get("name") else "")
        state = row.get("status") or "recorded in the database"
        for category, text in categories.items():
            if category == TERMS_CATEGORY:
                findings.append(doctor.Finding(
                    "account_terms_not_accepted", row["profile_id"],
                    f"{detail} of {owners} is stopped at the Terms of Service ({state}). {OWNER_HINT_TERMS} "
                    f"Reported from: {text[:180]}",
                    fixable=False, fix=None,
                ))
            else:
                findings.append(doctor.Finding(
                    "account_human_check_pending", row["profile_id"],
                    f"{detail} of {owners} is stopped at a human check ({category}, {state}); a person has to "
                    f"pass it in that window. AATM does not automate it. Reported from: {text[:180]}",
                    fixable=False, fix=None,
                ))
    return findings


def check_windows(conn: sqlite3.Connection, bridge: dict[str, dict[str, Any]] | None) -> list[doctor.Finding]:
    """The extra checks, as the single callback ``doctor.run_checks`` expects."""
    return check_windows_on_wrong_repo(conn, bridge) + check_accounts_pending_human_checks(conn, bridge)


# --------------------------------------------------------------------------------------- backup
def backup_before_fix(db_path: Path, reason: str) -> Path:
    """The app's backup helper when the venv has it, else a stdlib online backup. Never skipped.

    A doctor that cannot fix because a virtualenv is broken is not much use, so it does not depend on
    the backend to make the copy first. Either way the copy is written 0600 and the newest 20 are kept.
    """
    try:
        return doctor.backup_before_fix(db_path, reason)
    except Exception:  # any import or app-helper failure falls back; a failed backup never proceeds
        return _stdlib_backup(db_path, reason)


def _stdlib_backup(db_path: Path, reason: str) -> Path:
    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"\W+", "_", reason)
    target = backup_dir / f"aatm-doctor-{stamp}-{slug}.db"
    src = sqlite3.connect(db_path, timeout=10)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    target.chmod(0o600)
    backups = sorted(backup_dir.glob("aatm-doctor-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in backups[BACKUP_KEEP:]:
        stale.unlink(missing_ok=True)
    return target


def read_bridge_file(path: Path) -> dict[str, dict[str, Any]] | None:
    """The same shape the bridge gives, read from a file instead: for an offline or CI report."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"aatm doctor: cannot read {path}: {exc}", file=sys.stderr)
        return None
    if isinstance(data, dict) and "profiles" in data:
        data = data["profiles"]
    if isinstance(data, list):
        return {str(p["profile_id"]): p for p in data if isinstance(p, dict) and p.get("profile_id")}
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    return None


# --------------------------------------------------------------------------------------- report
def checks_run(bridge: dict[str, dict[str, Any]] | None) -> list[str]:
    """Which checks this run actually executed (the two window-state ones need a live bridge)."""
    skipped = set() if bridge is not None else {"window_connected_worker_offline", "window_script_outdated"}
    return sorted([name for name in STATE_CHECKS if name not in skipped] + list(WINDOW_CHECKS))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """The owner-facing extras: counts, the required-check proof, and the Terms alarm."""
    findings = report["findings"]
    by_check: dict[str, int] = {}
    for item in findings:
        by_check[item["check"]] = by_check.get(item["check"], 0) + 1
    present = set(by_check)
    ran = checks_run(None if not report["bridge_checked"] else {})
    report["checks_run"] = ran
    report["required_checks"] = list(REQUIRED_CHECKS)
    report["required_checks_run"] = [name for name in REQUIRED_CHECKS if name in ran]
    report["missing_required_checks"] = [name for name in REQUIRED_CHECKS if name not in ran]
    report["summary"] = {
        "findings": len(findings),
        "fixable": sum(1 for item in findings if item["fixable"]),
        "fixed": len(report["fixed"]),
        "by_check": by_check,
    }
    report["pending_human"] = {
        "terms_accounts": sorted({item["subject"] for item in findings
                                  if item["check"] == "account_terms_not_accepted"}),
        "other_checks": sorted({item["subject"] for item in findings
                                if item["check"] == "account_human_check_pending"}),
        "note": "report only — AATM never accepts terms, solves a CAPTCHA or signs in for a human (E25).",
    }
    report["safe_fix_policy"] = {
        "fixed_checks": sorted({item["check"] for item in report["fixed"]}),
        "never_fixed": sorted(present & {"window_on_wrong_repo", "account_terms_not_accepted",
                                         "account_human_check_pending", "task_assigned_to_manager",
                                         "queued_task_stuck", "window_script_outdated",
                                         "manager_job_stuck_needs_human"}),
        "backup_required_before_any_fix": True,
    }
    return report


def diagnose(db_path: Path, *, now: datetime, bridge: dict[str, dict[str, Any]] | None,
             script_version: str | None, fix: bool) -> dict[str, Any]:
    """``doctor.diagnose`` with this file's window checks and backup strategy added. No copied logic."""
    report = doctor.diagnose(db_path, now=now, bridge=bridge, script_version=script_version, fix=fix,
                             extra=check_windows, backup_fn=backup_before_fix)
    return summarize(report)


def alert_messages(report: dict[str, Any]) -> dict[str, str]:
    """Arabic one-liners for the watchdog: what to tell the owner, keyed so repeats can be de-duped."""
    out: dict[str, str] = {}
    for item in report["findings"]:
        key = f"{item['check']}:{item['subject']}"
        if item["check"] == "account_terms_not_accepted":
            out[key] = f"حساب لم يقبل الشروط بعد: النافذة {item['subject']} — القبول مرة واحدة بيد المالك، ولا تقبله AATM عنه."
        elif item["check"] == "account_human_check_pending":
            out[key] = f"تحقق بشري معلق: النافذة {item['subject']} تحتاج شخصًا لمتابعة الصفحة."
        elif item["check"] == "window_on_wrong_repo":
            out[key] = (f"نافذة على المستودع الخطأ: {item['subject']} — حدّد المستودع الصحيح قبل أي إرسال "
                        "(E34)؛ لا تُصلح آليًا لأن ذلك يتطلب نقرة في المتصفح.")
        elif item["fixable"]:
            out[key] = f"حالة قابلة للإصلاح الآمن: {item['check']} — {item['subject']} (شغّل aatm doctor --fix)."
        else:
            out[key] = f"يحتاج قرارًا بشريًا: {item['check']} — {item['subject']}."
    return out


# --------------------------------------------------------------------------------------- cli
def write_log_line(report: dict[str, Any]) -> None:
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    summary = report["summary"]
    line = (f"{report['checked_at']} doctor: {summary['findings']} finding(s), "
            f"{summary['fixable']} fixable, {summary['fixed']} fixed"
            + (f" [{', '.join(sorted(summary['by_check']))}]" if summary["by_check"] else "")
            + (f"; terms waiting on: {', '.join(report['pending_human']['terms_accounts'])}"
               if report["pending_human"]["terms_accounts"] else "")
            + "\n")
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(line)


def print_report(report: dict[str, Any]) -> None:
    print(f"AATM doctor — {report['checked_at']} — database {report['database']}")
    print(f"  checks run ({len(report['checks_run'])}): {', '.join(report['checks_run'])}")
    if not report["bridge_checked"]:
        print("  note: the bridge did not answer (or --no-bridge was given); the window-state checks were skipped.")
    for item in report["fixed"]:
        print(f"  FIXED   {item['check']} {item['subject']}: {item['fix']} (backup {item['backup_path']})")
    for item in report["findings"]:
        tag = "FIXABLE" if item["fixable"] else "REPORT "
        print(f"  {tag} {item['check']} {item['subject']}: {item['message']}")
    if report["pending_human"]["terms_accounts"]:
        print("  ACCOUNTS WAITING ON THE TERMS OF SERVICE (once, by the owner — E25):")
        for profile_id in report["pending_human"]["terms_accounts"]:
            print(f"    - {profile_id}")
    if not report["findings"]:
        print("  OK: no problems found.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--fix", action="store_true", help="back up, then repair the safe cases only")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--log", action="store_true", help=f"append one summary line to {_log_file()}")
    parser.add_argument("--once", action="store_true", help="accepted for the watchdog (the doctor always runs once)")
    parser.add_argument("--db", type=Path, help="database file (default: DATABASE_URL from .env, else data/aatm.db)")
    parser.add_argument("--no-bridge", action="store_true", help="skip the checks that need the live bridge")
    parser.add_argument("--bridge-file", type=Path,
                        help="read the window list from this JSON instead of the live bridge "
                             "({'profiles': [...]} or {profile_id: {...}}) — for an offline report")
    parser.add_argument("--alerts", action="store_true",
                        help="print only {check:subject -> message} as JSON, for the watchdog to notify")
    parser.add_argument("--quiet", action="store_true", help="print nothing (with --json the exit code still tells)")
    args = parser.parse_args(argv)

    env = doctor.read_env(ROOT_DIR / ".env")
    db_path = args.db or doctor.database_path(env)
    if not db_path.exists():
        print(f"aatm doctor: database not found: {db_path}", file=sys.stderr)
        return 2
    if args.bridge_file:
        bridge = read_bridge_file(args.bridge_file)
    else:
        bridge = None if args.no_bridge else doctor.fetch_bridge_windows(env)
    started = time.monotonic()
    try:
        report = diagnose(db_path, now=datetime.now(UTC), bridge=bridge,
                          script_version=doctor.current_script_version(), fix=args.fix)
    except doctor.BackupRefused as exc:
        print(f"aatm doctor: refusing to fix: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"aatm doctor: cannot read the database: {exc}", file=sys.stderr)
        return 2
    report["duration_seconds"] = round(time.monotonic() - started, 2)

    if args.alerts:
        print(json.dumps(alert_messages(report), ensure_ascii=False))
    elif args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif not args.quiet:
        print_report(report)
    if args.log:
        try:
            write_log_line(report)
        except OSError as exc:  # a missing log file must never fail a health check
            print(f"aatm doctor: could not write {_log_file()}: {exc}", file=sys.stderr)
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
