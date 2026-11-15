import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app


@pytest.fixture(scope="module")
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="module")
async def auth_headers(client: AsyncClient):
    settings = get_settings()
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_health_and_dashboard_overview(client: AsyncClient):
    h = await client.get("/api/health")
    assert h.status_code == 200
    assert h.json()["status"] == "ok"

    ov = await client.get("/api/v1/dashboard/overview")
    assert ov.status_code == 200
    data = ov.json()
    assert data["kpis"]["total_agents"] == 5
    assert len(data["divisions"]) == 12
    assert data["kpis"]["total_providers"] == 3
    assert data["kpis"]["total_browsers"] == 4


async def test_multilingual_preferences_and_auth_guard(client: AsyncClient, auth_headers):
    unauth = await client.post("/api/v1/orchestration/cycle")
    assert unauth.status_code == 401

    for lang in ("ar", "fa", "en"):
        res = await client.patch("/api/v1/auth/preferences", headers=auth_headers, json={"language": lang})
        assert res.status_code == 200
        assert res.json()["language"] == lang


async def test_task_dag_idempotency_and_orchestration(client: AsyncClient, auth_headers):
    projects = (await client.get("/api/v1/projects")).json()
    project_id = projects[0]["id"]

    # Create parent task with idempotency key
    t1 = await client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "division_code": "D08",
            "title": "Parent Prerequisite Task",
            "priority": "critical",
            "idempotency_key": "pytest-parent-01",
        },
    )
    assert t1.status_code == 201
    parent_id = t1.json()["id"]

    # Duplicate idempotency key returns same task
    t1_dup = await client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "division_code": "D08",
            "title": "Duplicate Attempt",
            "idempotency_key": "pytest-parent-01",
        },
    )
    assert t1_dup.json()["id"] == parent_id

    # Dependent child task
    t2 = await client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "division_code": "D08",
            "title": "Dependent Child Task",
            "priority": "high",
            "dependencies": [parent_id],
        },
    )
    assert t2.status_code == 201
    child_id = t2.json()["id"]

    # Run orchestration cycle — parent dispatched, child still queued due to dependency
    cyc1 = await client.post("/api/v1/orchestration/cycle", headers=auth_headers)
    assert cyc1.status_code == 200
    child_state = [t for t in (await client.get("/api/v1/tasks")).json() if t["id"] == child_id][0]
    assert child_state["status"] == "queued"

    # Complete parent -> child becomes dispatchable
    comp = await client.patch(
        f"/api/v1/tasks/{parent_id}",
        headers=auth_headers,
        json={"status": "completed", "result_summary": "Done in pytest"},
    )
    assert comp.status_code == 200
    assert comp.json()["status"] == "completed"

    cyc2 = await client.post("/api/v1/orchestration/cycle", headers=auth_headers)
    assert cyc2.status_code == 200
    child_state_2 = [t for t in (await client.get("/api/v1/tasks")).json() if t["id"] == child_id][0]
    assert child_state_2["status"] == "in_progress"

    # Complete child so worker returns to idle
    await client.patch(f"/api/v1/tasks/{child_id}", headers=auth_headers, json={"status": "completed"})


async def test_providers_and_browser_profiles_lifecycle(client: AsyncClient, auth_headers):
    hc = await client.post("/api/v1/providers/health-check", headers=auth_headers)
    assert hc.status_code == 200
    assert len(hc.json()) == 3

    dry = await client.post(
        "/api/v1/providers/anthropic-claude/test",
        headers=auth_headers,
        json={"prompt": "Verify routing", "dry_run": True},
    )
    assert dry.status_code == 200
    assert dry.json()["success"] is True

    usage = await client.get("/api/v1/providers/usage")
    assert usage.status_code == 200
    assert len(usage.json()["logs"]) >= 1

    # Browser launch, lock conflict, navigate, crash recovery, stop
    launch = await client.post(
        "/api/v1/browsers/profile-frontend-preview/launch",
        headers=auth_headers,
        json={"mode": "visible", "url": "http://localhost:5173", "owner": "worker-2"},
    )
    assert launch.status_code == 200
    assert launch.json()["status"] == "running"
    assert "<svg" in launch.json()["last_snapshot_svg"]

    conflict = await client.post(
        "/api/v1/browsers/profile-frontend-preview/launch",
        headers=auth_headers,
        json={"mode": "background", "owner": "worker-3"},
    )
    assert conflict.status_code == 409

    rec = await client.post("/api/v1/browsers/profile-frontend-preview/recover", headers=auth_headers)
    assert rec.status_code == 200
    assert rec.json()["crash_count"] >= 1
    assert rec.json()["status"] == "running"

    stop = await client.post("/api/v1/browsers/profile-frontend-preview/stop", headers=auth_headers)
    assert stop.status_code == 200
    assert stop.json()["status"] == "stopped"


async def test_memory_git_protected_branch_approval_and_qa(client: AsyncClient, auth_headers):
    mem = await client.get("/api/v1/memory?q=protected")
    assert mem.status_code == 200
    assert len(mem.json()) >= 1

    git_info = await client.get("/api/v1/git/status")
    assert git_info.status_code == 200
    workflows = git_info.json()["workflows"]
    assert len(workflows) >= 1

    # Protected merge blocked until approved
    wf_id = workflows[0]["id"]
    blocked = await client.post(f"/api/v1/git/workflows/{wf_id}/merge", headers=auth_headers)
    assert blocked.status_code == 200
    assert blocked.json()["allowed"] is False

    # Approve via Approval Center
    approvals = (await client.get("/api/v1/approvals?status=pending")).json()
    assert len(approvals) >= 1
    dec = await client.post(
        f"/api/v1/approvals/{approvals[0]['id']}/decide",
        headers=auth_headers,
        json={"decision": "approved", "review_note": "Verified by QA"},
    )
    assert dec.status_code == 200
    assert dec.json()["status"] == "approved"

    allowed = await client.post(f"/api/v1/git/workflows/{wf_id}/merge", headers=auth_headers)
    assert allowed.status_code == 200
    assert allowed.json()["allowed"] is True

    # QA & Self-Healing
    qa = await client.get("/api/v1/qa/summary")
    assert qa.status_code == 200
    assert qa.json()["security_scan"]["status"] == "passed"

    heal = await client.post("/api/v1/qa/self-heal", headers=auth_headers)
    assert heal.status_code == 200

    # Manager Chat directive
    chat = await client.post(
        "/api/v1/chat",
        headers=auth_headers,
        json={"content": "orchestrate and report team telemetry"},
    )
    assert chat.status_code == 201
    assert "Manager Directive Acknowledged" in chat.json()["manager_reply"]["content"]
