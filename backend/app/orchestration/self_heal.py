"""Self-healing for worker state (known issues E1 and E3).

A worker is only handed new work when it is `idle`. Cancelling or merging a task used to leave its worker
`working` on that task, and a finished task's Arena session stayed open and kept the worker reserved. These
helpers free the worker and close the session as soon as the task is finished, and they run again at the start
of every orchestration cycle so a state that was left behind is repaired without anyone pressing a button.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import Agent, ArenaSession, AuditLog, Task

logger = logging.getLogger(__name__)

FINISHED_TASK_STATUSES = frozenset({"completed", "done", "merged", "cancelled", "failed"})


def task_is_finished(task: Task | None) -> bool:
    """A missing task counts as finished: nothing can still be running for it."""
    return task is None or task.status in FINISHED_TASK_STATUSES


async def release_worker_for_task(db: AsyncSession, task: Task) -> bool:
    """Free the worker that was running `task`, if it is still bound to it. Returns True when it changed."""
    if not task.assigned_agent_id:
        return False
    worker = await db.get(Agent, task.assigned_agent_id)
    if worker is None or worker.current_task_id != task.id:
        return False
    worker.current_task_id = None
    worker.status = "idle"
    return True


async def heal_orphaned_state(db: AsyncSession) -> dict[str, Any]:
    """Repair workers bound to finished tasks and Arena sessions still open for finished tasks."""
    released: list[str] = []
    closed: list[str] = []

    workers = (await db.execute(select(Agent).where(Agent.current_task_id.is_not(None)))).scalars().all()
    for worker in workers:
        task = await db.get(Task, worker.current_task_id)
        if task_is_finished(task):
            worker.current_task_id = None
            if worker.status == "working":
                worker.status = "idle"
            released.append(worker.id)
        elif task is not None and task.status == "queued" and not task.lock_owner and worker.status == "working":
            # 2026-11-26: a backend restart put the running task back in the queue but left its worker "working",
            # so the worker never got it again (doctor: worker_working_without_task). Free it; dispatch follows.
            worker.current_task_id = None
            worker.status = "idle"
            released.append(worker.id)

    sessions = (await db.execute(select(ArenaSession).where(ArenaSession.is_open.is_(True)))).scalars().all()
    for session in sessions:
        if not session.task_id:
            continue
        task = await db.get(Task, session.task_id)
        if task is not None and task.status in FINISHED_TASK_STATUSES:
            session.is_open = False
            session.phase = "closed"
            closed.append(session.id)

    requeued = await requeue_transient_follow_ups(db)

    if released or closed or requeued:
        db.add(AuditLog(actor="self-healing", action="self_heal.orphaned_state", resource_type="scheduler",
                        resource_id="orchestration", details={"released_workers": released, "closed_sessions": closed,
                                                              "requeued_follow_ups": requeued},
                        severity="info"))
        await db.commit()
        logger.info("Self-heal released %d worker(s) and closed %d session(s)", len(released), len(closed))
    return {"released_workers": released, "closed_sessions": closed, "requeued_follow_ups": requeued}


# Live 2026-11-26: worker-4's correction of PR #12 failed after 3 quick retries because its Arena page did not
# send the text ("send_failed"), all within one minute; nothing picked it up again, so PR #12 waited forever.
TRANSIENT_ARENA_ERRORS = ("send_failed", "Arena Agent timeout", "did not finish 'selector_test'", "offline")
FOLLOW_UP_HEAL_REQUEUES = 2
FOLLOW_UP_HEAL_WAIT = timedelta(minutes=5)


async def requeue_transient_follow_ups(db: AsyncSession, *, now: datetime | None = None) -> list[str]:
    """Give a review follow-up that failed on a passing Arena page error a new start after a pause.

    Only follow-ups (their PR waits for them), only Arena page errors, at most FOLLOW_UP_HEAL_REQUEUES times,
    and only FOLLOW_UP_HEAL_WAIT after the failure, so a window that keeps failing is not hammered."""
    now = now or utcnow()
    requeued: list[str] = []
    failed = (await db.execute(select(Task).where(Task.status == "failed", Task.task_type == "follow_up"))).scalars().all()
    for task in failed:
        error = task.error_message or ""
        if not any(marker in error for marker in TRANSIENT_ARENA_ERRORS):
            continue
        checkpoints = list(task.checkpoints or [])
        if sum(1 for c in checkpoints if c.get("event") == "heal_requeued") >= FOLLOW_UP_HEAL_REQUEUES:
            continue
        changed = task.updated_at
        if changed is not None and changed.tzinfo is None:
            changed = changed.replace(tzinfo=timezone.utc)
        if changed is not None and now - changed < FOLLOW_UP_HEAL_WAIT:
            continue
        task.status, task.retry_count, task.lock_owner = "queued", 0, None
        task.checkpoints = checkpoints + [{"ts": now.isoformat(), "event": "heal_requeued", "after_error": error[:200]}]
        requeued.append(task.id)
    return requeued
