"""P0 / E32: Resume continues this window's current task only; another blocked task stays blocked."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.api.routes.arena import resume_after_human
from app.db.base import utcnow
from app.models import Agent, Project, Task
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures


def _blocked(project_id: str, agent_id: str, title: str) -> Task:
    return Task(project_id=project_id, title=title, description="x", status="blocked", assigned_agent_id=agent_id,
                execution_mode="manual", checkpoints=[{"ts": utcnow().isoformat(), "event": "needs_human"}])


@pytest.mark.asyncio
async def test_resume_requeues_only_the_current_task(env):  # noqa: F811
    async with env() as db:
        project = Project(name="Resume", slug="resume", repo_path="/tmp/none", default_branch="main")
        db.add(project)
        await db.flush()
        agent = await db.get(Agent, "worker-3")
        current, other = _blocked(project.id, agent.id, "current"), _blocked(project.id, agent.id, "other project task")
        db.add_all([current, other])
        await db.flush()
        agent.current_task_id = current.id
        agent.status = "needs_human"
        await db.commit()
        profile_id, current_id, other_id = agent.browser_profile_id, current.id, other.id
    async with env() as db:
        result = await resume_after_human(profile_id, SimpleNamespace(email="owner@test"), db)
        assert result["requeued_tasks"] == [current_id]
        assert result["skipped_tasks"] == [other_id]
        assert (await db.get(Task, current_id)).status == "queued"
        assert (await db.get(Task, other_id)).status == "blocked"
