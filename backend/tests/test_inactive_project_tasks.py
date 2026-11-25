"""A task of a missing, completed or disabled project is never created through the API or handed to a worker.

Found on 2026-11-25: after all projects were removed, a task posted with an unknown project id was accepted
(201) and dispatched to a worker within seconds; the repository guard was the only thing that stopped it.
"""

# ruff: noqa: F811 - pytest fixtures are re-exported from other test modules
from __future__ import annotations

import pytest
from sqlalchemy import select
from test_e14_project_picker import _client, chat_app  # noqa: F401 - fixture
from test_e_dashboard import env  # noqa: F401 - fixture

from app.models import Agent, Project, Task
from app.orchestration.scheduler import TaskScheduler


@pytest.mark.asyncio
async def test_api_refuses_a_task_for_an_unknown_project(chat_app) -> None:
    async with await _client(chat_app) as client:
        response = await client.post("/api/v1/tasks", json={"project_id": "no-such-project", "title": "probe"})
    assert response.status_code == 404
    async with chat_app() as db:
        assert (await db.execute(select(Task).where(Task.title == "probe"))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["disabled", "completed"])
async def test_api_refuses_a_task_for_an_inactive_project(chat_app, status) -> None:
    async with chat_app() as db:
        project = Project(name="Paused", slug=f"paused-{status}", status=status)
        db.add(project)
        await db.commit()
        project_id = project.id
    async with await _client(chat_app) as client:
        response = await client.post("/api/v1/tasks", json={"project_id": project_id, "title": "probe"})
    assert response.status_code == 409 and status in response.json()["detail"]


@pytest.mark.asyncio
async def test_api_still_creates_a_task_for_an_active_project(chat_app) -> None:
    async with chat_app() as db:
        project = Project(name="Live", slug="live", status="active")
        db.add(project)
        await db.commit()
        project_id = project.id
    async with await _client(chat_app) as client:
        response = await client.post("/api/v1/tasks", json={"project_id": project_id, "title": "real work"})
    assert response.status_code == 201


async def test_scheduler_skips_tasks_of_missing_or_inactive_projects(env):
    async with env() as db:
        live = Project(name="Live", slug="live", status="active")
        paused = Project(name="Paused", slug="paused", status="disabled")
        db.add_all([live, paused])
        await db.flush()
        for w in (await db.execute(select(Agent).where(Agent.role == "worker"))).scalars().all():
            w.status, w.current_task_id = "idle", None
        ok = Task(project_id=live.id, title="Live work", description="Create backend/app/api/live.py", status="queued")
        off = Task(project_id=paused.id, title="Paused work", description="Create backend/app/api/paused.py", status="queued")
        gone = Task(project_id="no-such-project", title="Orphan", description="Create backend/app/api/orphan.py", status="queued")
        db.add_all([ok, off, gone])
        await db.commit()
        ids = {ok.id, off.id, gone.id}
        ok_id, off_id, gone_id = ok.id, off.id, gone.id
    async with env() as db:
        await TaskScheduler().run_orchestration_cycle(db, only_task_ids=ids)
    async with env() as db:
        assert (await db.get(Task, ok_id)).status != "queued"
        assert (await db.get(Task, off_id)).status == "queued"
        assert (await db.get(Task, gone_id)).status == "queued"
