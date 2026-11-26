"""E1 / E3 regression: a finished task must not leave its worker busy or its Arena session open."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.models import Agent, ArenaSession, BrowserProfile, Project, Task
from app.orchestration.self_heal import heal_orphaned_state, release_worker_for_task


@pytest.fixture
async def db_session(monkeypatch, tmp_path) -> AsyncSession:
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "BROWSER_PROFILES_DIR", tmp_path / "browser_profiles")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        yield db


async def _busy_worker_with_task(db: AsyncSession, status: str) -> tuple[Agent, Task]:
    project = (await db.execute(select(Project))).scalars().first()
    worker = await db.get(Agent, "worker-1")
    task = Task(project_id=project.id, title="t", status=status, assigned_agent_id=worker.id)
    db.add(task)
    await db.flush()
    worker.status = "working"
    worker.current_task_id = task.id
    await db.commit()
    return worker, task


@pytest.mark.asyncio
async def test_cancelling_the_running_task_frees_its_worker(db_session):
    worker, task = await _busy_worker_with_task(db_session, "in_progress")
    task.status = "cancelled"
    assert await release_worker_for_task(db_session, task) is True
    await db_session.commit()
    await db_session.refresh(worker)
    assert worker.status == "idle"
    assert worker.current_task_id is None


@pytest.mark.asyncio
async def test_release_does_not_touch_a_worker_running_a_different_task(db_session):
    worker, task = await _busy_worker_with_task(db_session, "cancelled")
    other = Task(project_id=task.project_id, title="other", status="in_progress", assigned_agent_id=worker.id)
    db_session.add(other)
    await db_session.flush()
    worker.current_task_id = other.id
    assert await release_worker_for_task(db_session, task) is False
    assert worker.current_task_id == other.id


@pytest.mark.asyncio
async def test_heal_frees_a_worker_left_busy_on_a_finished_task(db_session):
    worker, _task = await _busy_worker_with_task(db_session, "cancelled")
    result = await heal_orphaned_state(db_session)
    await db_session.refresh(worker)
    assert worker.id in result["released_workers"]
    assert worker.status == "idle"
    assert worker.current_task_id is None


@pytest.mark.asyncio
async def test_heal_leaves_a_worker_on_a_running_task_alone(db_session):
    worker, _task = await _busy_worker_with_task(db_session, "in_progress")
    result = await heal_orphaned_state(db_session)
    await db_session.refresh(worker)
    assert result["released_workers"] == []
    assert worker.status == "working"


@pytest.mark.asyncio
async def test_heal_closes_an_arena_session_of_a_finished_task(db_session):
    worker, task = await _busy_worker_with_task(db_session, "done")
    profile = BrowserProfile(id="profile-test-heal", name="p", profile_dir="/tmp/profile-test-heal")
    db_session.add(profile)
    await db_session.flush()
    session = ArenaSession(agent_id=worker.id, profile_id=profile.id, task_id=task.id, is_open=True, phase="reviewing")
    db_session.add(session)
    await db_session.commit()

    result = await heal_orphaned_state(db_session)
    await db_session.refresh(session)
    assert session.id in result["closed_sessions"]
    assert session.is_open is False
    assert session.phase == "closed"


@pytest.mark.asyncio
async def test_completing_the_running_task_frees_its_worker(db_session):
    """E1: the completion path (complete_task) also leaves the worker idle and unbound."""
    from app.orchestration.scheduler import task_scheduler

    worker, task = await _busy_worker_with_task(db_session, "in_progress")
    await task_scheduler.complete_task(db_session, task, "done in test")
    await db_session.refresh(worker)
    assert worker.status == "idle"
    assert worker.current_task_id is None


@pytest.mark.asyncio
async def test_failing_the_running_task_frees_its_worker(db_session):
    """E1: the failure path (fail_and_retry_or_escalate) also leaves the worker idle and unbound."""
    from app.orchestration.scheduler import task_scheduler

    worker, task = await _busy_worker_with_task(db_session, "in_progress")
    await task_scheduler.fail_and_retry_or_escalate(db_session, task, "boom")
    await db_session.refresh(worker)
    assert worker.status == "idle"
    assert worker.current_task_id is None


async def test_heal_frees_a_worker_whose_task_went_back_to_the_queue(db_session):
    """2026-11-26: after a backend restart worker-3 stayed "working" on a task that was queued again."""
    worker, task = await _busy_worker_with_task(db_session, "queued")
    result = await heal_orphaned_state(db_session)
    assert worker.id in result["released_workers"]
    await db_session.refresh(worker)
    assert worker.status == "idle" and worker.current_task_id is None


async def test_heal_leaves_a_worker_whose_queued_task_is_being_dispatched(db_session):
    worker, task = await _busy_worker_with_task(db_session, "queued")
    task.lock_owner = worker.id
    await db_session.commit()
    assert worker.id not in (await heal_orphaned_state(db_session))["released_workers"]


async def test_a_follow_up_failed_on_an_arena_send_error_gets_a_new_start_after_a_pause(db_session):
    """Live 2026-11-26: PR #12's correction failed 3 times in one minute on "send_failed" and was never retried."""
    from datetime import timedelta

    from app.db.base import utcnow
    from app.orchestration.self_heal import FOLLOW_UP_HEAL_REQUEUES, requeue_transient_follow_ups

    project = (await db_session.execute(select(Project))).scalars().first()
    error = "ArenaRunnerError: Arena Agent send_failed: text was typed but the page did not send it"
    task = Task(project_id=project.id, title="Address review on PR #12", status="failed", task_type="follow_up",
                error_message=error, retry_count=4, assigned_agent_id="worker-4")
    other = Task(project_id=project.id, title="Address review on PR #3", status="failed", task_type="follow_up",
                 error_message="tests failed", assigned_agent_id="worker-4")
    db_session.add_all([task, other])
    await db_session.commit()
    soon = utcnow() + timedelta(minutes=1)
    assert await requeue_transient_follow_ups(db_session, now=soon) == [], "not at once: the window gets a pause"
    later = utcnow() + timedelta(minutes=6)
    for _ in range(FOLLOW_UP_HEAL_REQUEUES):
        assert await requeue_transient_follow_ups(db_session, now=later) == [task.id]
        assert task.status == "queued" and task.retry_count == 0
        task.status = "failed"
    assert await requeue_transient_follow_ups(db_session, now=later) == [], "a window that keeps failing is not hammered"
    assert other.status == "failed", "a real failure stays failed"
