"""Item E / Phase 7 — dashboard data is computed from real state (never mocked)."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.events import event_bus
from app.core.security import create_access_token
from app.db.base import Base, utcnow
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import Agent, AgentMessage, AuditLog, BrowserProfile, ChatMessage, Project, ProviderUsageLog, Task, User
from app.runtime.agent_loop import AgentLoop

FAKE_GH_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture()
async def env(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase7.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await seed_initial_data(db)
        await db.execute(update(Task).values(status="completed"))
        await db.commit()

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    yield factory
    app.dependency_overrides.pop(get_db, None)
    await engine.dispose()


async def _client_headers(factory) -> dict[str, str]:
    async with factory() as db:
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        admin.must_change_password = False
        await db.commit()
        return {"Authorization": f"Bearer {create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)}"}


async def _states(client, headers, project_id) -> dict[str, str]:
    r = await client.get(f"/api/v1/projects/{project_id}/timeline", headers=headers)
    assert r.status_code == 200, r.text
    return {s["id"]: s["state"] for s in r.json()["steps"]}


async def test_eight_step_timeline_lights_up_from_real_task_and_pr_state(env):
    factory = env
    headers = await _client_headers(factory)
    async with factory() as db:
        project = Project(name="Shop", slug="shop", default_branch="main")
        db.add(project)
        await db.commit()
        pid = project.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        states = await _states(client, headers, pid)
        assert list(states) == ["request", "analyze_split", "distribute", "execute", "collect", "pull_request", "review_merge", "completed"]
        assert set(states.values()) == {"pending"}, "no evidence -> nothing lit"

        async with factory() as db:
            db.add(ChatMessage(project_id=pid, sender_type="user", sender_id="owner", content="ابنِ متجراً"))
            tasks = [Task(project_id=pid, title=f"T{i}", description="d", status="queued", division_code="D02") for i in range(2)]
            db.add_all(tasks)
            await db.commit()
            ids = [t.id for t in tasks]
        states = await _states(client, headers, pid)
        assert states["request"] == "done" and states["analyze_split"] == "done" and states["distribute"] == "pending"

        async with factory() as db:
            for tid in ids:
                t = await db.get(Task, tid)
                t.assigned_agent_id, t.status, t.started_at, t.worktree_path = "worker-1", "in_progress", utcnow(), "/tmp/wt"
            await db.commit()
        states = await _states(client, headers, pid)
        assert states["distribute"] == "done" and states["execute"] == "active" and states["collect"] == "pending"

        async with factory() as db:
            t = await db.get(Task, ids[0])
            t.status, t.commit_sha, t.pr_number = "pr_open", "abc", 11
            await db.commit()
        states = await _states(client, headers, pid)
        assert states["execute"] == "active" and states["collect"] == "active" and states["pull_request"] == "active"

        async with factory() as db:
            t = await db.get(Task, ids[1])
            t.status, t.commit_sha, t.pr_number = "pr_open", "def", 12
            db.add(Task(project_id=pid, title="Review T0", description="r", status="queued", task_type="review", parent_task_id=ids[0], division_code="D02"))
            await db.commit()
        states = await _states(client, headers, pid)
        assert [states[k] for k in ("execute", "collect", "pull_request")] == ["done"] * 3
        assert states["review_merge"] == "active" and states["completed"] == "pending"

        async with factory() as db:
            for tid in ids:
                (await db.get(Task, tid)).status = "done"
            (await db.get(Project, pid)).completed_at = utcnow()
            await db.commit()
        r = await client.get(f"/api/v1/projects/{pid}/timeline", headers=headers)
        assert {s["state"] for s in r.json()["steps"]} == {"done"} and r.json()["current_step"] is None
        assert (await client.get("/api/v1/projects/nope/timeline", headers=headers)).status_code == 404


async def test_home_counters_employee_cards_recovery_and_agent_log(env):
    factory = env
    headers = await _client_headers(factory)
    async with factory() as db:
        project = Project(name="P", slug="p", default_branch="main")
        db.add(project)
        await db.flush()
        running = Task(project_id=project.id, title="Build API", description="d", status="in_progress", assigned_agent_id="worker-1", division_code="D02", progress_percent=40)
        failed = Task(project_id=project.id, title="Broken", description="d", status="failed", retry_count=3, max_retries=3, error_message="tests failed", division_code="D02")
        retried = Task(project_id=project.id, title="Flaky", description="d", status="queued", retry_count=1, division_code="D02")
        pr = Task(project_id=project.id, title="With PR", description="d", status="pr_open", pr_number=5, division_code="D02")
        db.add_all([running, failed, retried, pr])
        await db.flush()
        worker = await db.get(Agent, "worker-1")
        worker.status, worker.current_task_id = "working", running.id
        db.add(ProviderUsageLog(provider_id=worker.provider_id, agent_id="worker-1", task_id=running.id, model_name="m", estimated_cost_usd=0.25))
        db.add(AuditLog(actor="system", action="employee.replaced", resource_type="agent", resource_id="worker-2", details={}))
        profile = await db.get(BrowserProfile, (await db.get(Agent, "worker-4")).browser_profile_id)
        profile.connector_status, profile.needs_human_reason = "needs_human", "verification page shown"
        message = AgentMessage(sender_id="manager", recipient_id="worker-1", task_id=running.id, instructions="Build API")
        db.add(message)
        await db.commit()

        # A real iteration through the agent loop's persistence path -> log row + redacted live event.
        seen: list[dict] = []
        original_emit = event_bus.emit

        async def spy(event_type, payload):
            seen.append({"type": event_type, "payload": payload})
            return await original_emit(event_type, payload)

        event_bus.emit = spy  # type: ignore[method-assign]
        try:
            await AgentLoop(session_factory=factory)._persist_iteration(db, message, running, worker, 2, "run", f"pytest passed; token={FAKE_GH_TOKEN}")
        finally:
            event_bus.emit = original_emit  # type: ignore[method-assign]
    live = [e for e in seen if e["type"] == "agent.iteration"]
    assert live and live[0]["payload"]["agent_id"] == "worker-1" and live[0]["payload"]["action"] == "run"
    assert FAKE_GH_TOKEN not in str(seen)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        home = (await client.get("/api/v1/dashboard/home", headers=headers)).json()
        assert [a["id"] for a in home["architecture"]] == ["dashboard", "orchestrator", "profiles", "results"]
        c = home["counters"]
        assert c["active_employees"] == 1 and c["open_prs"] == 1 and c["needs_human"] == 1
        assert c["cost_today_usd"] == 0.25 and c["tasks_by_status"]["failed"] == 1

        cards = {e["id"]: e for e in (await client.get("/api/v1/dashboard/employees", headers=headers)).json()["employees"]}
        w1 = cards["worker-1"]
        assert w1["current_task"]["title"] == "Build API" and w1["cost_usd"] == 0.25
        assert w1["last_action"]["summary"].startswith("Iteration 2: run") and FAKE_GH_TOKEN not in w1["last_action"]["summary"]
        assert len({e["color"] for e in cards.values()}) == len(cards)

        log = (await client.get("/api/v1/dashboard/agent-log", headers=headers)).json()["entries"]
        assert log[-1]["agent_id"] == "worker-1" and FAKE_GH_TOKEN not in str(log)

        rec = (await client.get("/api/v1/dashboard/recovery", headers=headers)).json()
        assert [t["title"] for t in rec["failed_tasks"]] == ["Broken"]
        assert [t["title"] for t in rec["retried_tasks"]] == ["Flaky"]
        assert rec["replacements"][0]["resource_id"] == "worker-2"
        assert rec["needs_human"][0]["reason"] == "verification page shown"

        assert (await client.get("/api/v1/dashboard/home")).status_code == 401
