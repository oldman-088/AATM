"""E17 — the doctor: every check against a seeded database, and --fix repairs only the safe cases (after a backup)."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.db.base import Base
from app.models import Agent, ArenaSession, BrowserProfile, ManagerJob, Project, Task

DOCTOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ubuntu" / "doctor.py"
_spec = importlib.util.spec_from_file_location("aatm_doctor", DOCTOR_PATH)
doctor = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = doctor  # dataclasses looks the module up by name
_spec.loader.exec_module(doctor)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(hours=2)
CURRENT = "2.3.0"  # the connector script version this checkout ships (app.browser.arena_connector)
BRIDGE = {
    "profile-b": {"connected": True, "script_version": "2.1.0"},   # connected, old script, worker-b offline
    "profile-c": {"connected": False, "script_version": CURRENT},
}
ALL_CHECKS = {
    "worker_working_without_task",
    "window_connected_worker_offline",
    "session_open_for_finished_task",
    "task_assigned_to_manager",
    "queued_task_stuck",
    "window_script_outdated",
    "manager_job_stuck_needs_human",
}


def _seed(db_file: Path) -> None:
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        project = Project(name="P", slug="p", repo_path=".")
        db.add(project)
        db.flush()
        db.add(Agent(id="manager-0", name="M", specialty="test", role="manager", status="idle", last_heartbeat=OLD))
        db.add(BrowserProfile(id="profile-b", name="B", profile_dir="x"))
        db.add(BrowserProfile(id="profile-c", name="C", profile_dir="x"))
        # worker-a: working with nothing running (safe to fix)
        db.add(Agent(id="worker-a", name="A", specialty="test", role="worker", status="working", current_task_id=None, last_heartbeat=OLD))
        # worker-b: offline while its window (profile-b) is connected (safe to fix)
        db.add(Agent(id="worker-b", name="B", specialty="test", role="worker", status="offline", browser_profile_id="profile-b", last_heartbeat=OLD))
        # worker-c: healthy, working on a running task
        db.add(Agent(id="worker-c", name="C", specialty="test", role="worker", status="working", browser_profile_id="profile-c", last_heartbeat=OLD))
        # worker-e: working with nothing running, but an Arena session is still open (report only)
        db.add(Agent(id="worker-e", name="E", specialty="test", role="worker", status="working", last_heartbeat=OLD))
        db.add(Agent(id="worker-d", name="D", specialty="test", role="worker", status="idle", last_heartbeat=OLD))
        db.flush()

        running = Task(project_id=project.id, division_code="D01", title="running", description="d", status="in_progress",
                       assigned_agent_id="worker-c", dependencies=[], updated_at=NOW, created_at=NOW)
        done = Task(project_id=project.id, division_code="D01", title="done", description="d", status="completed",
                    assigned_agent_id="worker-d", dependencies=[], updated_at=OLD, created_at=OLD)
        to_manager = Task(project_id=project.id, division_code="D01", title="to manager", description="d", status="queued",
                          assigned_agent_id="manager-0", dependencies=[], updated_at=NOW, created_at=NOW)
        stuck = Task(project_id=project.id, division_code="D01", title="stuck", description="d", status="queued",
                     assigned_agent_id="worker-d", dependencies=[], updated_at=OLD, created_at=OLD)
        waiting = Task(project_id=project.id, division_code="D01", title="waiting", description="d", status="queued",
                       assigned_agent_id="worker-d", dependencies=[], updated_at=OLD, created_at=OLD)
        fresh = Task(project_id=project.id, division_code="D01", title="fresh", description="d", status="queued",
                     assigned_agent_id="worker-d", dependencies=[], updated_at=NOW, created_at=NOW)
        db.add_all([running, done, to_manager, stuck, waiting, fresh])
        db.flush()
        # waiting depends on the running task (not done yet): not stuck.
        waiting.dependencies = [running.id]
        # stuck depends on the completed task: its prerequisites are done, so it should be running by now.
        stuck.dependencies = [done.id]
        db.flush()
        # ORM onupdate rewrites updated_at on every change; pin the queued-since time explicitly.
        db.execute(text("UPDATE tasks SET updated_at = :t WHERE title IN ('stuck', 'waiting')"), {"t": OLD})
        db.add(ArenaSession(agent_id="worker-d", profile_id="profile-d", task_id=done.id, active_task_id=done.id,
                            phase="closed", is_open=True, events=[]))
        db.add(ArenaSession(agent_id="worker-e", profile_id="profile-e", task_id=None, phase="working", is_open=True, events=[]))
        db.add(ManagerJob(kind="plan", status="needs_human", request_text="g", context={}, error="needs a human",
                          updated_at=OLD, created_at=OLD))
        db.add(ManagerJob(kind="plan", status="needs_human", request_text="fresh", context={},
                          updated_at=NOW, created_at=NOW))
        db.commit()
    engine.dispose()


@pytest.fixture()
def seeded(tmp_path: Path) -> Path:
    db_file = tmp_path / "doctor.db"
    _seed(db_file)
    return db_file


def _findings(db_file: Path, bridge=BRIDGE, script_version=CURRENT) -> list:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        return doctor.run_checks(conn, now=NOW, bridge=bridge, script_version=script_version)
    finally:
        conn.close()


def _by_check(findings) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for f in findings:
        out.setdefault(f.check, set()).add(f.subject)
    return out


def test_every_check_finds_its_seeded_case(seeded):
    found = _by_check(_findings(seeded))
    assert set(found) == ALL_CHECKS
    assert found["worker_working_without_task"] == {"worker-a", "worker-e"}
    assert found["window_connected_worker_offline"] == {"worker-b"}
    assert found["window_script_outdated"] == {"profile-b"}
    assert len(found["session_open_for_finished_task"]) == 1
    assert len(found["task_assigned_to_manager"]) == 1
    assert len(found["queued_task_stuck"]) == 1  # only "stuck": "waiting" depends on an unfinished task, "fresh" is recent
    assert len(found["manager_job_stuck_needs_human"]) == 1


def test_healthy_worker_and_recent_or_waiting_tasks_are_not_flagged(seeded):
    found = _findings(seeded)
    subjects = {f.subject for f in found}
    assert "worker-c" not in subjects  # working on a running task
    assert "worker-d" not in subjects  # idle
    titles = " ".join(f.message for f in found)
    assert "'waiting'" not in titles and "'fresh'" not in titles


def test_unreachable_bridge_skips_the_window_checks(seeded):
    found = _by_check(_findings(seeded, bridge=None))
    assert "window_connected_worker_offline" not in found
    assert "window_script_outdated" not in found


def test_fix_repairs_only_the_safe_cases_after_a_backup(seeded):
    report = doctor.diagnose(seeded, now=NOW, bridge=BRIDGE, script_version=CURRENT, fix=True)

    fixed = {(item["check"], item["subject"]) for item in report["fixed"]}
    assert fixed == {
        ("worker_working_without_task", "worker-a"),
        ("window_connected_worker_offline", "worker-b"),
        ("session_open_for_finished_task", report["fixed"][2]["subject"]),
    }
    # Every repair names the backup taken before it.
    backup = Path(report["fixed"][0]["backup_path"])
    assert backup.exists() and backup.parent == seeded.parent / "backups"
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600

    conn = sqlite3.connect(seeded)
    try:
        assert conn.execute("SELECT status FROM agents WHERE id='worker-a'").fetchone()[0] == "idle"
        assert conn.execute("SELECT status FROM agents WHERE id='worker-b'").fetchone()[0] == "idle"
        # Not safe: an open session blocks worker-e's repair, and the manager and stuck-task findings are for a person.
        assert conn.execute("SELECT status FROM agents WHERE id='worker-e'").fetchone()[0] == "working"
        assert conn.execute("SELECT assigned_agent_id FROM tasks WHERE title='to manager'").fetchone()[0] == "manager-0"
        assert conn.execute("SELECT status FROM tasks WHERE title='stuck'").fetchone()[0] == "queued"
        assert conn.execute("SELECT status FROM manager_jobs WHERE request_text='g'").fetchone()[0] == "needs_human"
        assert conn.execute("SELECT is_open FROM arena_sessions WHERE agent_id='worker-d'").fetchone()[0] == 0
    finally:
        conn.close()

    remaining = {f["check"] for f in report["findings"]}
    assert remaining == {"worker_working_without_task", "task_assigned_to_manager", "queued_task_stuck",
                         "window_script_outdated", "manager_job_stuck_needs_human"}
    assert all(not f["fixable"] for f in report["findings"] if f["check"] == "worker_working_without_task" and f["subject"] == "worker-e")


def test_fix_changes_nothing_when_the_backup_fails(seeded, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(doctor, "backup_before_fix", _boom)
    with pytest.raises(OSError):
        doctor.diagnose(seeded, now=NOW, bridge=BRIDGE, script_version=CURRENT, fix=True)
    conn = sqlite3.connect(seeded)
    try:
        assert conn.execute("SELECT status FROM agents WHERE id='worker-a'").fetchone()[0] == "working"
        assert conn.execute("SELECT status FROM agents WHERE id='worker-b'").fetchone()[0] == "offline"
        assert conn.execute("SELECT is_open FROM arena_sessions WHERE agent_id='worker-d'").fetchone()[0] == 1
    finally:
        conn.close()


def test_report_only_run_changes_nothing(seeded):
    doctor.diagnose(seeded, now=NOW, bridge=BRIDGE, script_version=CURRENT, fix=False)
    conn = sqlite3.connect(seeded)
    try:
        assert conn.execute("SELECT status FROM agents WHERE id='worker-a'").fetchone()[0] == "working"
    finally:
        conn.close()


def test_cli_exit_codes_and_clean_database(tmp_path, seeded):
    assert doctor.main(["--db", str(seeded), "--no-bridge", "--json"]) == 1
    clean = tmp_path / "clean.db"
    engine = create_engine(f"sqlite:///{clean}")
    Base.metadata.create_all(engine)
    engine.dispose()
    assert doctor.main(["--db", str(clean), "--no-bridge"]) == 0
    assert doctor.main(["--db", str(tmp_path / "missing.db")]) == 2


def test_script_version_comparison():
    assert doctor.version_tuple("2.1.0") < doctor.version_tuple("2.2.0")
    assert doctor.version_tuple("2.2") == (2, 2, 0)
    assert doctor.version_tuple(None) is None
    assert doctor.current_script_version() == CURRENT
