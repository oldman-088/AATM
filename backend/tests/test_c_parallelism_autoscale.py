"""Item C / Phase 5 — true parallelism, approval-gated scale-up, scale-down, wake bus.

Acceptance (docs/AATM_BUILD_PLAN.md §Phase 5):
* default team = 1 manager + 4 workers, each with its own browser profile;
* 4 tasks run with overlapping execution while the API (manager) stays responsive and the
  device heartbeat stays < 200 ms;
* 20 tasks -> scale-up proposal(s) -> human approval -> extra workers -> all 20 complete,
  each executed exactly once, with no "database is locked";
* idle extra workers are scaled down, default workers never are.

The model is a deterministic scripted router with real latency (asyncio.sleep); everything
else (scheduler, atomic claims, agent loop, Approval Center API, SQLite WAL) is real.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.db.base import Base, utcnow
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import Agent, AIProvider, ApprovalRequest, BrowserProfile, ExecutionDevice, Project, Task, User
from app.orchestration.autoscale import EXTRA_TEMPLATE, autoscale_service
from app.orchestration.scheduler import TaskScheduler
from app.providers.base import ProviderRequest, ProviderResponse
from app.queue import reset_wake_bus
from app.queue.base import InProcessWakeBus, task_queue_name
from app.runtime.agent_loop import AgentLoop


class _SlowRouter:
    def __init__(self, latency: float = 0.3) -> None:
        self.latency = latency
        self.calls: list[dict[str, Any]] = []

    async def route_and_execute(self, db, request: ProviderRequest, preferred_provider_id=None):
        started = time.monotonic()
        await asyncio.sleep(self.latency)
        self.calls.append({"agent_id": request.agent_id, "task_id": request.task_id, "start": started, "end": time.monotonic()})
        return ProviderResponse(
            success=True,
            provider_id=preferred_provider_id or "anthropic-claude",
            model="claude-sonnet-4-5",
            content=json.dumps({"action": "done", "summary": f"Finished {request.task_id}."}),
            input_tokens=10,
            output_tokens=5,
            estimated_cost_usd=0.0,
            latency_ms=self.latency * 1000,
        )


@pytest.fixture()
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase5.db'}", connect_args={"timeout": 30})
    from sqlalchemy import event

    from app.db.session import _apply_sqlite_pragmas

    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "AUTO_SCALE_MODE", "approval")
    monkeypatch.setattr(settings, "SCALE_UP_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(settings, "SCALE_UP_BACKLOG_THRESHOLD", 3)
    monkeypatch.setattr(settings, "MAX_CONCURRENT_WORKERS", 10)
    monkeypatch.setenv("TEST_C_CLAUDE_API_KEY", "sk-ant-phase5-dummy")
    reset_wake_bus(InProcessWakeBus())

    async with factory() as db:
        await seed_initial_data(db)
        prov = await db.get(AIProvider, "anthropic-claude")
        prov.is_enabled = True
        prov.credential_ref = "env:TEST_C_CLAUDE_API_KEY"
        await db.execute(update(Task).values(status="completed"))
        await db.execute(update(Agent).values(status="idle", current_task_id=None))
        for agent in (await db.execute(select(Agent))).scalars():
            agent.provider_id = "anthropic-claude"
        project = Project(name="Phase5", slug="phase5", repo_path=str(tmp_path / "plain"), default_branch="main", config={})
        (tmp_path / "plain").mkdir()
        db.add(project)
        await db.commit()
        project_id = project.id

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    yield factory, project_id
    app.dependency_overrides.pop(get_db, None)
    reset_wake_bus(None)
    await engine.dispose()


async def _add_tasks(factory, project_id: str, n: int) -> list[str]:
    async with factory() as db:
        tasks = [
            Task(project_id=project_id, title=f"Parallel task {i}", description="Do it.", status="queued",
                 execution_mode="automatic", priority="medium", division_code="D02")
            for i in range(n)
        ]
        db.add_all(tasks)
        await db.commit()
        return [t.id for t in tasks]


async def _admin_headers(factory) -> dict[str, str]:
    async with factory() as db:
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        admin.must_change_password = False
        await db.commit()
        return {"Authorization": f"Bearer {create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)}"}


async def _status_counts(factory, ids: list[str]) -> dict[str, int]:
    async with factory() as db:
        rows = (await db.execute(select(Task.status, func.count()).where(Task.id.in_(ids)).group_by(Task.status))).all()
        return {s: c for s, c in rows}


def test_default_team_is_one_manager_plus_four_parallel_workers(monkeypatch):
    for key in ("AGENT_MAX_PARALLEL", "ARENA_BRIDGE_MAX_CONCURRENCY", "DEFAULT_ACTIVE_WORKERS", "AUTO_SCALE_MODE",
                "MAX_CONCURRENT_WORKERS", "SCALE_UP_BACKLOG_THRESHOLD", "SCALE_UP_WAIT_SECONDS", "SCALE_DOWN_IDLE_MINUTES"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)
    assert s.DEFAULT_ACTIVE_WORKERS == 8
    assert s.AGENT_MAX_PARALLEL == 8
    assert s.ARENA_BRIDGE_MAX_CONCURRENCY == 1  # per profile, not global
    assert (s.AUTO_SCALE_MODE, s.SCALE_UP_BACKLOG_THRESHOLD, s.SCALE_UP_WAIT_SECONDS) == ("approval", 3, 120.0)
    assert (s.MAX_CONCURRENT_WORKERS, s.SCALE_DOWN_IDLE_MINUTES) == (25, 30.0)


async def test_seed_has_manager_and_four_workers_each_with_own_profile(env):
    factory, _ = env
    async with factory() as db:
        agents = list((await db.execute(select(Agent))).scalars())
        assert [a.role for a in agents].count("manager") == 1
        workers = [a for a in agents if a.role == "worker"]
        assert len(workers) == 8
        profiles = {w.browser_profile_id for w in workers}
        assert len(profiles) == 8 and None not in profiles
        for pid in profiles:
            assert await db.get(BrowserProfile, pid) is not None
        status = await autoscale_service.status(db)
        assert status["label"] == "8 default + 0 extra"


async def test_four_tasks_overlap_while_manager_stays_responsive(env):
    factory, project_id = env
    ids = await _add_tasks(factory, project_id, 4)
    router = _SlowRouter(latency=0.6)
    scheduler = TaskScheduler(provider_router=router)
    async with factory() as db:
        cycle = await scheduler.run_orchestration_cycle(db)
        assert len(cycle["dispatched"]) == 4
        assert len({d["worker_id"] for d in cycle["dispatched"]}) == 4

    loop = AgentLoop(session_factory=factory, provider_router=router, poll_seconds=0.05)
    assert loop.max_parallel == 8
    runner = asyncio.create_task(loop.run_forever())
    headers = await _admin_headers(factory)
    latencies: list[float] = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await asyncio.sleep(0.15)
            for _ in range(5):
                t0 = time.monotonic()
                res = await client.get("/api/v1/agents", headers=headers)
                latencies.append(time.monotonic() - t0)
                assert res.status_code == 200
                t0 = time.monotonic()
                await client.get("/api/health")
                latencies.append(time.monotonic() - t0)
                await asyncio.sleep(0.05)
        deadline = time.monotonic() + 15
        while (await _status_counts(factory, ids)).get("completed", 0) < 4 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
    finally:
        await loop.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert (await _status_counts(factory, ids)) == {"completed": 4}
    calls = [c for c in router.calls if c["task_id"] in ids]
    assert len(calls) == 4
    assert max(c["start"] for c in calls) < min(c["end"] for c in calls), "all 4 model turns must overlap"
    assert max(latencies) < 0.2, latencies


async def test_device_heartbeat_latency_under_200ms_during_parallel_work(env):
    from app.runtime.device_auth import issue_device_token

    factory, project_id = env
    ids = await _add_tasks(factory, project_id, 4)
    router = _SlowRouter(latency=0.5)
    async with factory() as db:
        await TaskScheduler(provider_router=router).run_orchestration_cycle(db)
        db.add(ExecutionDevice(id="node-hb", name="node-hb", status="online", token_version=1))
        await db.commit()
    token = issue_device_token("node-hb", 1)
    loop = AgentLoop(session_factory=factory, provider_router=router, poll_seconds=0.05)
    runner = asyncio.create_task(loop.run_forever())
    timings = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await asyncio.sleep(0.1)
            for _ in range(5):
                t0 = time.monotonic()
                res = await client.post("/api/v1/devices/node-hb/heartbeat", headers={"X-AATM-Device-Token": token}, json={})
                timings.append(time.monotonic() - t0)
                assert res.status_code == 200, res.text
                await asyncio.sleep(0.05)
    finally:
        await loop.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert max(timings) < 0.2, timings
    assert ids


async def test_twenty_tasks_scale_up_with_approval_and_complete_exactly_once(env, caplog):
    factory, project_id = env
    caplog.set_level(logging.WARNING)
    ids = await _add_tasks(factory, project_id, 20)
    router = _SlowRouter(latency=0.15)
    scheduler = TaskScheduler(provider_router=router)
    headers = await _admin_headers(factory)

    async with factory() as db:
        first = await scheduler.run_orchestration_cycle(db)
    assert len(first["dispatched"]) == 8
    assert first["scaling"]["proposed"] >= 1, first["scaling"]
    async with factory() as db:
        pending = list((await db.execute(select(ApprovalRequest).where(ApprovalRequest.action_type == "scale_up"))).scalars())
        assert len(pending) == 1 and pending[0].status == "pending"
        # Nothing is created before a human decides.
        assert (await db.execute(select(func.count()).select_from(Agent).where(Agent.role_template == EXTRA_TEMPLATE))).scalar() == 0
        # A second cycle does not open a duplicate proposal.
        again = await scheduler.run_orchestration_cycle(db)
        assert again["scaling"].get("pending") is True

    loop = AgentLoop(session_factory=factory, provider_router=router, poll_seconds=0.02, max_parallel=10)
    runner = asyncio.create_task(loop.run_forever())
    approved: set[str] = set()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                async with factory() as db:
                    await scheduler.run_orchestration_cycle(db)
                    open_reqs = list((await db.execute(select(ApprovalRequest).where(
                        ApprovalRequest.action_type == "scale_up", ApprovalRequest.status == "pending"))).scalars())
                for req in open_reqs:
                    res = await client.post(f"/api/v1/approvals/{req.id}/decide", headers=headers,
                                            json={"decision": "approved", "review_note": "more hands"})
                    assert res.status_code == 200, res.text
                    approved.add(req.id)
                if (await _status_counts(factory, ids)).get("completed", 0) == 20:
                    break
                await asyncio.sleep(0.05)
    finally:
        await loop.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert (await _status_counts(factory, ids)) == {"completed": 20}
    executed = [c["task_id"] for c in router.calls if c["task_id"] in ids]
    assert sorted(executed) == sorted(ids), "every task executed exactly once"
    async with factory() as db:
        extras = list((await db.execute(select(Agent).where(Agent.role_template == EXTRA_TEMPLATE))).scalars())
        workers = (await db.execute(select(func.count()).select_from(Agent).where(Agent.role == "worker"))).scalar()
        approvals = list((await db.execute(select(ApprovalRequest).where(ApprovalRequest.id.in_(approved)))).scalars())
    assert approved and extras and workers <= 10
    assert all(a.payload.get("executed") for a in approvals)
    assert {a.id for a in extras}.issubset({wid for a in approvals for wid in a.payload["created"]})
    assert all(e.browser_profile_id == f"profile-{e.id}" for e in extras)
    assert extra_overlap(router.calls, ids) > 4, "extra workers actually ran in parallel"
    assert "database is locked" not in caplog.text


def extra_overlap(calls: list[dict[str, Any]], ids: list[str]) -> int:
    spans = [(c["start"], c["end"]) for c in calls if c["task_id"] in ids]
    return max(sum(1 for s, e in spans if s <= t < e) for t, _ in spans)


async def test_scale_up_respects_off_mode_automatic_mode_and_max_workers(env, monkeypatch):
    factory, project_id = env
    settings = get_settings()
    await _add_tasks(factory, project_id, 12)
    async with factory() as db:
        await db.execute(update(Agent).where(Agent.role == "worker").values(status="working"))
        await db.commit()
        monkeypatch.setattr(settings, "AUTO_SCALE_MODE", "off")
        assert (await autoscale_service.evaluate(db))["proposed"] == 0
        monkeypatch.setattr(settings, "AUTO_SCALE_MODE", "automatic")
        monkeypatch.setattr(settings, "MAX_CONCURRENT_WORKERS", 10)
        monkeypatch.setattr(settings, "SCALE_UP_STEP", 4)
        res = await autoscale_service.evaluate(db)
        assert res["created"] == ["worker-x1", "worker-x2"]  # capped at MAX_CONCURRENT_WORKERS=10 (8 defaults + 2)
        await db.execute(update(Agent).where(Agent.role == "worker").values(status="working"))
        await db.commit()
        res = await autoscale_service.evaluate(db)
        assert res.get("at_capacity") is True and res["created"] == []
        monkeypatch.setattr(settings, "SCALE_UP_WAIT_SECONDS", 3600.0)
        monkeypatch.setattr(settings, "MAX_CONCURRENT_WORKERS", 25)
        assert (await autoscale_service.evaluate(db))["created"] == []  # tasks have not waited long enough


async def test_rejected_or_reused_scale_approval_creates_nothing(env):
    factory, _ = env
    headers = await _admin_headers(factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/scaling/workers", headers=headers, json={"count": 2})
        assert res.status_code == 202
        aid = res.json()["approval_id"]
        res = await client.post(f"/api/v1/approvals/{aid}/decide", headers=headers, json={"decision": "rejected"})
        assert res.status_code == 200
        status = (await client.get("/api/v1/scaling/status", headers=headers)).json()
        assert status["extra_workers"] == 0 and status["default_workers"] == 8
        res = await client.post("/api/v1/scaling/workers", headers=headers, json={"count": 2})
        aid2 = res.json()["approval_id"]
        assert (await client.post(f"/api/v1/approvals/{aid2}/decide", headers=headers, json={"decision": "approved"})).status_code == 200
        status = (await client.get("/api/v1/scaling/status", headers=headers)).json()
        assert status["extra_workers"] == 2 and status["label"] == "8 default + 2 extra"
    async with factory() as db:
        approval = await db.get(ApprovalRequest, aid2)
        again = await autoscale_service.on_approval_decided(db, approval, actor="replay")
        assert again == {"ok": False, "code": "APPROVAL_ALREADY_USED"}
        assert (await autoscale_service.status(db))["extra_workers"] == 2


async def test_scale_down_retires_idle_extras_never_defaults(env, monkeypatch):
    factory, _ = env
    monkeypatch.setattr(get_settings(), "SCALE_DOWN_IDLE_MINUTES", 30.0)
    async with factory() as db:
        created = await autoscale_service.add_workers(db, 2, actor="test", reason="test")
        assert [a.id for a in created] == ["worker-x1", "worker-x2"]
        # Nobody has worked for an hour: only the extras are retired.
        retired = await autoscale_service.scale_down(db, now=utcnow() + timedelta(minutes=60))
        assert sorted(retired) == ["worker-x1", "worker-x2"]
        defaults = list((await db.execute(select(Agent).where(Agent.role == "worker", Agent.role_template != EXTRA_TEMPLATE))).scalars())
        assert len(defaults) == 8 and all(a.is_enabled for a in defaults)
        assert (await autoscale_service.status(db))["extra_workers"] == 0
        # Re-scaling reuses retired extras (and their profiles) instead of piling up rows.
        again = await autoscale_service.add_workers(db, 1, actor="test", reason="test")
        assert [a.id for a in again] == ["worker-x1"] and again[0].is_enabled
        # Recently active extras stay.
        assert await autoscale_service.scale_down(db, now=utcnow() + timedelta(minutes=5)) == []


async def test_sqlite_warning_above_three_employees(env, monkeypatch):
    factory, _ = env
    async with factory() as db:
        status = await autoscale_service.status(db)
        assert status["database_backend"] == "sqlite" and status["sqlite_warning"] is True
        monkeypatch.setattr(get_settings(), "DATABASE_URL", "postgresql+psycopg://u:p@db/aatm")
        assert (await autoscale_service.status(db))["sqlite_warning"] is False


async def test_wake_bus_wakes_idle_loop_before_poll_interval_and_duplicates_are_harmless(env):
    factory, project_id = env
    ids = await _add_tasks(factory, project_id, 1)
    router = _SlowRouter(latency=0.01)
    bus = InProcessWakeBus()
    reset_wake_bus(bus)
    loop = AgentLoop(session_factory=factory, provider_router=router, poll_seconds=5.0)
    runner = asyncio.create_task(loop.run_forever())
    try:
        await asyncio.sleep(0.2)  # loop is now parked waiting (poll = 5s)
        t0 = time.monotonic()
        async with factory() as db:
            cycle = await TaskScheduler(provider_router=router).run_orchestration_cycle(db)
        assert len(cycle["dispatched"]) == 1
        await bus.publish_task("device-local", ids[0])  # duplicate wake for the same task
        while (await _status_counts(factory, ids)).get("completed", 0) < 1 and time.monotonic() - t0 < 4:
            await asyncio.sleep(0.02)
        elapsed = time.monotonic() - t0
    finally:
        await loop.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert (await _status_counts(factory, ids)) == {"completed": 1}
    assert elapsed < 2.0, "woken by the bus, not by the 5s poll"
    assert loop.wakes_received >= 1
    assert [c["task_id"] for c in router.calls].count(ids[0]) == 1
    assert bus.depth(task_queue_name("device-local")) <= 1


async def test_per_profile_bridge_limit_and_per_provider_limit(monkeypatch):
    from app.providers import adapters
    from app.providers.concurrency import concurrency_status, provider_slot

    monkeypatch.setattr(get_settings(), "ARENA_BRIDGE_MAX_CONCURRENCY", 1)
    monkeypatch.delenv("ARENA_BRIDGE_MAX_CONCURRENCY", raising=False)
    active: dict[str, int] = {}
    peak: dict[str, int] = {}

    async def hold(key: str):
        async with adapters._bridge_semaphore(key):
            active[key] = active.get(key, 0) + 1
            peak[key] = max(peak.get(key, 0), active[key])
            await asyncio.sleep(0.05)
            active[key] -= 1

    t0 = time.monotonic()
    await asyncio.gather(*(hold(k) for k in ("acct-1", "acct-2", "acct-3", "acct-4", "acct-1")))
    assert peak == {"acct-1": 1, "acct-2": 1, "acct-3": 1, "acct-4": 1}
    assert time.monotonic() - t0 < 0.18  # 4 profiles ran in parallel; only acct-1 queued once

    monkeypatch.setattr(get_settings(), "PROVIDER_CONCURRENCY_LIMITS", "prov-a=2")
    in_flight = 0
    seen = 0

    async def call(pid: str):
        nonlocal in_flight, seen
        async with provider_slot(pid):
            in_flight += 1
            seen = max(seen, in_flight)
            await asyncio.sleep(0.03)
            in_flight -= 1

    await asyncio.gather(*(call("prov-a") for _ in range(6)))
    assert seen == 2
    assert concurrency_status()["prov-a"] == {"limit": 2, "in_flight": 0}


def test_load_test_script_passes_in_process():
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "load_test.py"), "--tasks", "10", "--latency", "0.3", "--max-workers", "8"],
        capture_output=True, text=True, timeout=180, cwd=root,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    report = json.loads(proc.stdout[proc.stdout.index("{"): proc.stdout.rindex("}") + 1])
    assert report["completed"] == 10 and report["duplicate_executions"] == 0 and report["database_locked_errors"] == 0
