"""P0 / E24+E38: the generic stalled-task requeue must not touch a task an open Arena session is working on."""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.db.base import utcnow
from app.models import Task
from app.orchestration.scheduler import task_scheduler
from test_arena_recovery import _session
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures


async def _age(db, task_id: str, minutes: int) -> None:
    task = await db.get(Task, task_id)
    task.updated_at = utcnow() - timedelta(minutes=minutes)
    await db.commit()


@pytest.mark.asyncio
async def test_long_arena_turn_is_not_requeued(env):  # noqa: F811
    async with env() as db:
        _, task_id = await _session(db, task_status="in_progress", phase="working", age_minutes=50)
        await _age(db, task_id, 50)
    async with env() as db:
        recovered = await task_scheduler.detect_and_recover_stalled_tasks(db)
        assert recovered == []
        assert (await db.get(Task, task_id)).status == "in_progress"


@pytest.mark.asyncio
async def test_task_without_session_is_still_requeued(env):  # noqa: F811
    async with env() as db:
        _, task_id = await _session(db, task_status="in_progress", phase="closed", age_minutes=50)
        await _age(db, task_id, 50)
        from app.models import ArenaSession
        from sqlalchemy import update
        await db.execute(update(ArenaSession).values(is_open=False))
        await db.commit()
    async with env() as db:
        recovered = await task_scheduler.detect_and_recover_stalled_tasks(db)
        assert recovered == [task_id]
