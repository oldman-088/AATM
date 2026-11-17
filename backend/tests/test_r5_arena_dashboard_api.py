"""R5 — backend data and controls behind the Arabic-first Arena Agent dashboard."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_d_arena_connector
from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_d_arena_connector import _admin_headers, arena_settings, env  # noqa: F401 - fixtures

from app.db.base import utcnow
from app.main import app
from app.models import Agent, ArenaSession, AuditLog, Project, Task


async def test_overview_lists_every_employee_with_session_task_and_human_events(env, arena_settings, monkeypatch):
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", "http://127.0.0.1:9/mock-arena/")
    monkeypatch.setattr(arena_settings, "ARENA_BRIDGE_URL", "http://127.0.0.1:9")
    async with env() as db:
        project = Project(name="R5", slug="r5", repo_path="/tmp/none", default_branch="main")
        db.add(project)
        await db.flush()
        workers = list((await db.execute(select(Agent).where(Agent.role == "worker", Agent.is_enabled.is_(True)))).scalars())
        task = Task(project_id=project.id, title="Add answer()", status="in_progress", assigned_agent_id=workers[0].id)
        done = Task(project_id=project.id, title="Old", status="done", assigned_agent_id=workers[0].id)
        db.add_all([task, done])
        await db.flush()
        db.add_all([
            ArenaSession(agent_id=workers[0].id, profile_id=workers[0].browser_profile_id, task_id=task.id, phase="needs_human",
                         page_state="needs_human", needs_human_reason="stalled", branch_name="arena/x", pr_url="https://github.com/a/b/pull/1",
                         keep_working_count=4, events=[{"ts": utcnow().isoformat(), "kind": "needs_human", "detail": "stalled"},
                                                       {"ts": utcnow().isoformat(), "kind": "keep_working"}]),
            ArenaSession(agent_id=workers[0].id, profile_id=workers[0].browser_profile_id, task_id=done.id, phase="merged", is_open=False),
        ])
        await db.commit()
    headers = await _admin_headers(env)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.put("/api/v1/arena/agents/mode", json={"mode": "agent"}, headers=headers)
        assert r.status_code == 200 and r.json()["count"] == len(workers)
        body = (await client.get("/api/v1/arena/agent/overview", headers=headers)).json()
    # R7: the manager has its own card and counts as one Arena (agent-mode) profile like any worker.
    assert len(body["employees"]) == len(workers) and body["counts"]["agent_mode_profiles"] == len(workers) + 1
    assert body["manager"]["agent_id"] == "manager-0" and body["manager"]["profile_name"] == "manager"
    row = next(e for e in body["employees"] if e["agent_id"] == workers[0].id)
    assert row["task"]["title"] == "Add answer()" and row["session"]["branch_name"] == "arena/x"
    assert row["session"]["keep_working_count"] == 4
    assert [e["kind"] for e in row["human_events"]] == ["needs_human"]
    assert body["task_distribution"] == {"in_progress": 1, "done": 1}
    assert body["counts"]["running_tasks"] == 1 and body["counts"]["completed_tasks"] == 1


async def test_submit_task_assigns_a_queued_task_to_the_employee_and_is_audited(env, arena_settings):
    async with env() as db:
        project = Project(name="R5b", slug="r5b", repo_path="/tmp/none", default_branch="main")
        db.add(project)
        await db.flush()
        queued = Task(project_id=project.id, title="Add greet()", status="queued", execution_mode="automatic")
        running = Task(project_id=project.id, title="Busy", status="in_progress")
        db.add_all([queued, running])
        await db.commit()
        queued_id, running_id = queued.id, running.id
    headers = await _admin_headers(env)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/arena/agents/worker-2/assign-task", json={"task_id": queued_id}, headers=headers)
        assert r.status_code == 200, r.text
        r = await client.post("/api/v1/arena/agents/worker-2/assign-task", json={"task_id": running_id}, headers=headers)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "task_not_queued"
        r = await client.post("/api/v1/arena/agents/nobody/assign-task", json={"task_id": queued_id}, headers=headers)
        assert r.status_code == 404
    async with env() as db:
        task = await db.get(Task, queued_id)
        assert task.assigned_agent_id == "worker-2" and task.execution_mode == "manual" and task.status == "queued"
        assert (await db.execute(select(AuditLog).where(AuditLog.action == "arena.task_assigned"))).scalars().first()
