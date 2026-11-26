"""Self-healing for worker state (known issues E1 and E3).

A worker is only handed new work when it is `idle`. Cancelling or merging a task used to leave its worker
`working` on that task, and a finished task's Arena session stayed open and kept the worker reserved. These
helpers free the worker and close the session as soon as the task is finished, and they run again at the start
of every orchestration cycle so a state that was left behind is repaired without anyone pressing a button.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

    if released or closed:
        db.add(AuditLog(actor="self-healing", action="self_heal.orphaned_state", resource_type="scheduler",
                        resource_id="orchestration", details={"released_workers": released, "closed_sessions": closed},
                        severity="info"))
        await db.commit()
        logger.info("Self-heal released %d worker(s) and closed %d session(s)", len(released), len(closed))
    return {"released_workers": released, "closed_sessions": closed}
