"""Regression tests: a forgotten human check or a finished task must not hold a worker forever."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures

from app.db.base import utcnow
from app.models import Agent, ArenaSession, AuditLog, Project, Task
from app.orchestration.arena_runner import ArenaAgentRunner, arena_reservations


async def _session(db, *, task_status: str, phase: str, age_minutes: int) -> tuple[str, str]:
    """Create one worker bound to one task with an open Arena session. Returns (session_id, task_id)."""
    project = Project(name="Recovery", slug="recovery", repo_path="/tmp/none", default_branch="main")
    db.add(project)
    await db.flush()
    agent = await db.get(Agent, "worker-1")
    task = Task(project_id=project.id, title="Recovery task", description="x", status=task_status,
                assigned_agent_id=agent.id, execution_mode="manual")
    db.add(task)
    await db.flush()
    stamp = utcnow() - timedelta(minutes=age_minutes)
    session = ArenaSession(agent_id=agent.id, profile_id=agent.browser_profile_id, task_id=task.id, active_task_id=task.id,
                           phase=phase, page_state="offline", submitted_at=stamp, last_state_change_at=stamp,
                           events=[], is_open=True)
    db.add(session)
    agent.status = "needs_human" if phase == "needs_human" else "working"
    agent.current_task_id = task.id
    await db.commit()
    return session.id, task.id


def _runner() -> ArenaAgentRunner:
    return ArenaAgentRunner(SimpleNamespace())


@pytest.mark.asyncio
async def test_expired_human_wait_releases_worker_and_keeps_task_blocked(env):  # noqa: F811
    async with env() as db:
        session_id, task_id = await _session(db, task_status="blocked", phase="needs_human", age_minutes=45)
    async with env() as db:
        released = await _runner().expire_stale_human_sessions(db)
        session = await db.get(ArenaSession, session_id)
        task = await db.get(Task, task_id)
        agent = await db.get(Agent, "worker-1")
        audit = list((await db.execute(select(AuditLog).where(AuditLog.action == "arena.needs_human_expired"))).scalars())
        assert released == ["worker-1"]
        assert session.is_open is False
        assert task.status == "blocked"          # a person still has to resolve it
        assert agent.status == "idle"
        assert len(audit) == 1
        assert await arena_reservations(db) == {}


@pytest.mark.asyncio
async def test_recent_human_wait_is_not_released(env):  # noqa: F811
    async with env() as db:
        session_id, _ = await _session(db, task_status="blocked", phase="needs_human", age_minutes=5)
    async with env() as db:
        assert await _runner().expire_stale_human_sessions(db) == []
        session = await db.get(ArenaSession, session_id)
        assert session.is_open is True
        assert (await db.get(Agent, "worker-1")).status == "needs_human"  # untouched: the wait is still recent


@pytest.mark.asyncio
async def test_session_of_a_finished_task_does_not_reserve_its_worker(env):  # noqa: F811
    async with env() as db:
        await _session(db, task_status="done", phase="reviewing", age_minutes=1)
    async with env() as db:
        assert await arena_reservations(db) == {}


@pytest.mark.asyncio
async def test_session_of_an_active_task_still_reserves_its_worker(env):  # noqa: F811
    async with env() as db:
        _, task_id = await _session(db, task_status="in_progress", phase="working", age_minutes=1)
    async with env() as db:
        assert await arena_reservations(db) == {"worker-1": task_id}
