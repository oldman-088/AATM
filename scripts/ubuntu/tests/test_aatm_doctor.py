"""The aatm doctor entry point: the four required checks, the safe-fix policy, and no duplication.

Every test here runs against a temp SQLite file with its own backup directory and log path, so
nothing touches data/aatm.db, .env or a running stack.
"""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "ubuntu" / "aatm_doctor.py"
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)

FOUR_REQUIRED_CHECKS = {
    "worker_working_without_task",
    "window_connected_worker_offline",
    "session_open_for_finished_task",
    "window_on_wrong_repo",
}

OLD = "2020-01-01 00:00:00"  # far past any grace window the checks use


# --------------------------------------------------------------------------------------------- fixtures
@pytest.fixture
def drift(aatm, db):
    """One database where each of the four required problems is present at the same time.

    worker-1 works with no task; worker-2 is offline while its window is connected; worker-3's
    session is still open for a finished task; worker-4's window sits on the wrong repository.
    """
    db.add("projects", id="p1", name="osint-app", repo_url="https://github.com/oldman-088/osint-app.git",
           repo_path=".", status="active")
    for agent, status, profile in (("worker-1", "working", "prof-1"), ("worker-2", "offline", "prof-2"),
                                   ("worker-3", "working", "prof-3"), ("worker-4", "working", "prof-4")):
        db.add("agents", id=agent, role="worker", status=status, current_task_id=None,
               last_heartbeat=OLD, browser_profile_id=profile)
        db.add("browser_profiles", id=profile, name=f"{agent} window", agent_id=agent,
               connector_status="online", needs_human_reason=None)
    db.add("tasks", id="t-finished", project_id="p1", title="shipped work", status="completed",
           assigned_agent_id="worker-3", task_type="implementation", dependencies="[]", updated_at=OLD)
    db.add("tasks", id="t-live", project_id="p1", title="live work", status="in_progress",
           assigned_agent_id="worker-4", task_type="implementation", dependencies="[]", updated_at=OLD)
    db.add("arena_sessions", id="s-3", agent_id="worker-3", profile_id="prof-3", task_id="t-finished",
           active_task_id="t-finished", project_id="p1", phase="reporting", is_open=1,
           page_state="idle", needs_human_reason=None, error_message=None)
    db.add("arena_sessions", id="s-4", agent_id="worker-4", profile_id="prof-4", task_id="t-live",
           active_task_id="t-live", project_id="p1", phase="needs_human", is_open=1,
           page_state="needs_human", error_message=None,
           needs_human_reason="repo_mismatch: selected repository is oldman-088/AATM, the task needs oldman-088/osint-app")
    return db


BRIDGE = {
    "prof-2": {"profile_id": "prof-2", "connected": True, "status": "ready", "agent_state": "idle"},
    "prof-3": {"profile_id": "prof-3", "connected": True, "status": "ready", "agent_state": "idle"},
    "prof-4": {"profile_id": "prof-4", "connected": True, "status": "needs_human",
               "agent_state": "needs_human", "needs_human_reason": "repo_mismatch"},
}


def diagnose(module, db, *, bridge=BRIDGE, fix=False):
    return module.diagnose(db.path, now=NOW, bridge=bridge, script_version="2.2.0", fix=fix)


def checks_of(report):
    return {item["check"] for item in report["findings"]}


# ----------------------------------------------------------------------------------- the four checks
def test_all_four_required_checks_fire_in_one_report(aatm, drift):
    """The acceptance list, in one run: state drift, a stale session and the wrong repository."""
    report = diagnose(aatm, drift)
    assert FOUR_REQUIRED_CHECKS <= checks_of(report), sorted(checks_of(report))
    repo = next(item for item in report["findings"] if item["check"] == "window_on_wrong_repo")
    assert repo["subject"] == "prof-4"
    assert "oldman-088/AATM" in repo["message"] and "oldman-088/osint-app" in repo["message"]


def test_json_output_names_all_four_required_checks(aatm, db, tmp_path):
    """``aatm_doctor --json`` proves the four checks ran, even when nothing is wrong.

    The window list comes from a file (``--bridge-file``) so this asserts the real CLI output without
    a live bridge, exactly as a report on a machine with the stack stopped would look.
    """
    windows = tmp_path / "windows.json"
    windows.write_text(json.dumps({"profiles": [
        {"profile_id": "prof-1", "connected": True, "status": "ready", "agent_state": "idle"}]}),
        encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "AATM_DOCTOR_BACKUP_DIR": str(tmp_path / "backups"), "AATM_DOCTOR_LOG": str(tmp_path / "doctor.log")}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--db", str(db.path), "--bridge-file", str(windows)],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr  # a clean database is a clean exit code
    report = json.loads(result.stdout)
    assert set(report["required_checks"]) == FOUR_REQUIRED_CHECKS
    for name in FOUR_REQUIRED_CHECKS:
        assert name in report["checks_run"], report["checks_run"]
    assert report["missing_required_checks"] == []
    assert report["findings"] == []
    assert report["summary"] == {"findings": 0, "fixable": 0, "fixed": 0, "by_check": {}}


def test_cli_json_exit_code_and_findings(aatm, drift, tmp_path):
    before = drift.bytes()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "AATM_DOCTOR_BACKUP_DIR": str(tmp_path / "backups"), "AATM_DOCTOR_LOG": str(tmp_path / "doctor.log")}
    windows = tmp_path / "windows.json"
    windows.write_text(json.dumps({"profiles": [
        {"profile_id": pid, **fields} for pid, fields in
        {"prof-2": {"connected": True, "status": "ready", "agent_state": "idle"},
         "prof-4": {"connected": True, "status": "needs_human", "needs_human_reason": "repo_mismatch"}}.items()]}),
        encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--log", "--db", str(drift.path), "--bridge-file", str(windows)],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, result.stderr  # findings reported, nothing repaired
    report = json.loads(result.stdout)
    assert FOUR_REQUIRED_CHECKS <= {item["check"] for item in report["findings"]}
    logged = (tmp_path / "doctor.log").read_text()
    assert "finding(s)" in logged and ", 0 fixed" in logged
    # --log alone must not turn a report into a write: the file is byte-identical
    assert drift.bytes() == before


# ------------------------------------------------------------------------------------- E34: the repo
def test_repo_mismatch_is_detected_and_never_fixed(aatm, drift):
    """The check that did not exist before: a window on the wrong repository (E34).

    Report-only on purpose. Correcting it means clicking the repository control of a live Arena
    chat, and a wrong click sends the task's prompt into the wrong repository — a human decision,
    or the dispatcher's (E34), not the doctor's.
    """
    report = diagnose(aatm, drift)
    finding = next(item for item in report["findings"] if item["check"] == "window_on_wrong_repo")
    assert finding["fixable"] is False and finding["fix"] is None
    assert "prof-4" in str(finding["subject"])
    assert "window_on_wrong_repo" in report["safe_fix_policy"]["never_fixed"]


def test_repo_mismatch_survives_a_fix_run_untouched(aatm, drift):
    """With --fix the safe rows change and the window does not: no write, no click, no automation."""
    before = drift.rows("arena_sessions")
    report = diagnose(aatm, drift, fix=True)
    assert report["fixed"], "the safe findings should have been repaired"
    assert all(item["check"] != "window_on_wrong_repo" for item in report["fixed"])
    assert "window_on_wrong_repo" in checks_of(report)  # still reported, still for a person
    s4 = next(row for row in drift.rows("arena_sessions") if row["id"] == "s-4")
    assert s4 == next(row for row in before if row["id"] == "s-4"), "the window's own row must not be touched"


def test_repo_codes_that_are_not_about_a_wrong_repository_are_ignored(aatm, db):
    """A needs_human for another reason is not a repository problem — no false alarm."""
    db.add("agents", id="worker-9", role="worker", status="idle", last_heartbeat=OLD, browser_profile_id="prof-9")
    db.add("browser_profiles", id="prof-9", name="worker-9 window", agent_id="worker-9",
           connector_status="online", needs_human_reason="captcha: complete the check")
    report = diagnose(aatm, db, bridge={"prof-9": {"profile_id": "prof-9", "connected": True,
                                                  "status": "needs_human",
                                                  "needs_human_reason": "captcha: complete the check"}})
    assert "window_on_wrong_repo" not in checks_of(report)
    assert "account_human_check_pending" in checks_of(report)


# --------------------------------------------------------------------------------- E25: the terms
def test_terms_of_service_are_listed_as_an_alarm_not_a_task(aatm, db):
    """E25: after setup the owner sees which accounts have not accepted the terms, once."""
    db.add("agents", id="worker-7", role="worker", status="idle", last_heartbeat=OLD, browser_profile_id="prof-7")
    db.add("browser_profiles", id="prof-7", name="worker-7 window", agent_id="worker-7",
           connector_status="needs_human", needs_human_reason="page shows: terms of service")
    report = diagnose(aatm, db, bridge={"prof-7": {"profile_id": "prof-7", "connected": True,
                                                  "status": "needs_human",
                                                  "needs_human_reason": "page shows: terms of service"}})
    finding = next(item for item in report["findings"] if item["check"] == "account_terms_not_accepted")
    assert finding["fixable"] is False
    assert report["pending_human"]["terms_accounts"] == ["prof-7"]
    assert "never accepts them for a human" in finding["message"]
    # the watchdog's alert text for the same finding, in the owner's language
    alerts = aatm.alert_messages(report)
    alert = alerts["account_terms_not_accepted:prof-7"]
    assert "لم يقبل الشروط" in alert
    assert "account_terms_not_accepted" in report["safe_fix_policy"]["never_fixed"]


def test_a_fix_run_never_touches_a_terms_or_captcha_stop(aatm, db):
    """A human check stays exactly where it was, even when other rows are repaired around it."""
    db.add("agents", id="worker-8", role="worker", status="offline", last_heartbeat=OLD, browser_profile_id="prof-8")
    db.add("browser_profiles", id="prof-8", name="worker-8 window", agent_id="worker-8",
           connector_status="needs_human", needs_human_reason="captcha: verify you are human")
    before = db.snapshot()
    report = diagnose(aatm, db, bridge={"prof-8": {"profile_id": "prof-8", "connected": True,
                                                  "status": "needs_human",
                                                  "needs_human_reason": "captcha: verify you are human"}}, fix=True)
    assert not any(item["check"].startswith("account_") for item in report["fixed"])
    assert db.rows("browser_profiles") == before["browser_profiles"]
    assert "account_human_check_pending" in checks_of(report)


# --------------------------------------------------------------------------------- the fix policy
def test_safe_fix_writes_nothing_when_the_backup_fails(aatm, db, monkeypatch):
    """The backup is not a courtesy: without it the fix is refused and the database is untouched."""
    db.add("agents", id="worker-1", role="worker", status="working", last_heartbeat=OLD, browser_profile_id=None)
    assert any(item["fixable"] for item in diagnose(aatm, db, bridge=None)["findings"])
    before = db.bytes()

    def broken(_path, _reason):
        raise OSError("disk full")

    monkeypatch.setattr(aatm.doctor, "backup_before_fix", broken)
    monkeypatch.setattr(aatm, "_stdlib_backup", broken)
    with pytest.raises(aatm.doctor.BackupRefused):
        diagnose(aatm, db, bridge=None, fix=True)
    assert db.bytes() == before
    assert list((db.path.parent / "backups").glob("*")) == []  # nothing written, not even a stale copy


def test_safe_fix_is_preceded_by_a_readable_backup(aatm, db):
    """One safe case, repaired after a private copy of the database that really contains the old row."""
    db.add("agents", id="worker-1", role="worker", status="working", last_heartbeat=OLD, browser_profile_id=None)
    report = diagnose(aatm, db, bridge=None, fix=True)
    assert [item["check"] for item in report["fixed"]] == ["worker_working_without_task"]
    assert db.rows("agents")[0]["status"] == "idle"
    backup = Path(report["fixed"][0]["backup_path"])
    assert backup.exists() and stat.S_IMODE(backup.stat().st_mode) == 0o600
    copied = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        assert copied.execute("SELECT status FROM agents WHERE id = 'worker-1'").fetchone()[0] == "working"
    finally:
        copied.close()


def test_report_only_run_leaves_the_database_byte_identical(aatm, drift):
    before = drift.bytes()
    diagnose(aatm, drift, fix=False)
    assert drift.bytes() == before


# ------------------------------------------------------------------ no duplicated check logic
def test_entry_point_delegates_the_state_checks_to_doctor(aatm, monkeypatch):
    """The four state checks live in doctor.py once: the entry point only composes and decorates."""
    called: dict[str, object] = {}

    def fake_diagnose(db_path, **kwargs):
        called["kwargs"] = kwargs
        return {"checked_at": "x", "database": str(db_path), "bridge_checked": True, "fixed": [],
                "findings": [], "checked_by": "doctor"}

    monkeypatch.setattr(aatm.doctor, "diagnose", fake_diagnose)
    report = aatm.diagnose(Path("ignored.db"), now=NOW, bridge={}, script_version=None, fix=False)
    assert report["checked_by"] == "doctor"  # doctor.py produced the findings, this file only added the shape
    extra = called["kwargs"]["extra"]
    assert extra is aatm.check_windows
    assert called["kwargs"]["backup_fn"] is aatm.backup_before_fix


@pytest.mark.parametrize("name", [
    "check_workers_working_without_task", "check_window_connected_worker_offline",
    "check_session_open_for_finished_task", "check_tasks_assigned_to_manager",
    "check_queued_tasks_stuck", "check_window_script_outdated", "check_manager_jobs_needs_human",
])
def test_state_checks_are_not_copied_into_the_entry_point(aatm, name):
    """Guard for the review: a copy of a doctor.py check here should fail this."""
    assert not hasattr(aatm, name), f"{name} must stay in doctor.py only"
    assert f"def {name}" not in (SCRIPT).read_text(encoding="utf-8")
    assert callable(getattr(aatm.doctor, name))


def test_doctor_still_works_on_its_own(aatm, db):
    """The library keeps its own CLI: the state-only report is still runnable and unchanged."""
    db.add("agents", id="worker-1", role="worker", status="working", last_heartbeat=OLD, browser_profile_id=None)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ubuntu/doctor.py"), "--json", "--no-bridge", "--db", str(db.path)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert [item["check"] for item in report["findings"]] == ["worker_working_without_task"]
    assert "window_on_wrong_repo" not in json.dumps(report)  # the entry point owns that one
