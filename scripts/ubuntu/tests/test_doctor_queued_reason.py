"""2026-11-26: a queued task that waits for a known, correct reason is not "stuck, needs a human"."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from conftest import load_script

NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC)
OLD = "2026-11-26 08:00:00"


def _check(db):
    doctor = load_script("doctor.py", "doctor_queued_reason_under_test")
    conn = sqlite3.connect(db.path)
    conn.row_factory = sqlite3.Row
    try:
        return {f.subject: f for f in doctor.check_queued_tasks_stuck(conn, NOW)}
    finally:
        conn.close()


def test_a_task_whose_worker_is_busy_is_not_reported(db):
    db.add("agents", id="worker-1", role="worker", status="working", current_task_id="t-run")
    db.add("tasks", id="t-run", project_id="p1", title="running", status="in_progress", assigned_agent_id="worker-1")
    db.add("tasks", id="t-next", project_id="p1", title="next", status="queued", assigned_agent_id="worker-1",
           dependencies="[]", updated_at=OLD)
    assert "t-next" not in _check(db)


def test_an_idle_worker_with_a_ready_task_is_still_reported_with_a_hint(db):
    db.add("agents", id="worker-8", role="worker", status="idle")
    db.add("agents", id="worker-1", role="worker", status="working", current_task_id="t-run")
    db.add("tasks", id="t-run", project_id="p1", title="running", status="in_progress", assigned_agent_id="worker-1")
    db.add("tasks", id="t-8", project_id="p1", title="tooling", status="queued", assigned_agent_id="worker-8",
           dependencies="[]", updated_at=OLD)
    found = _check(db)
    assert "t-8" in found and "T16" in found["t-8"].message
