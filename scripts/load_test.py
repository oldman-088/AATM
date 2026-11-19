#!/usr/bin/env python3
"""AATM Phase 5 load test.

Two modes:

  inprocess (default) - spins up the real scheduler, autoscaler, atomic-claim agent loop and
      Approval Center API against a throwaway database, with a *scripted* model that only
      sleeps (no provider is ever called, no spend). Queues N tasks, approves the scale-up
      proposals the autoscaler opens (printed as "SIMULATED HUMAN APPROVAL"), and checks:
        * every task completed exactly once (no duplicate execution)
        * no "database is locked"
        * API p95 latency while workers run < --max-api-ms
        * peak parallel model turns > the default team size when extras were approved

        .venv/bin/python scripts/load_test.py --tasks 20 --latency 0.2
        .venv/bin/python scripts/load_test.py --database-url postgresql+psycopg://aatm:<password>@127.0.0.1:5432/aatm_load

  http - read-only latency probe of a running backend (never creates tasks, so no spend):

        AATM_LOAD_TEST_TOKEN=<jwt> .venv/bin/python scripts/load_test.py --mode http --url http://127.0.0.1:8000

Exit code 0 = all checks passed, 1 = a check failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def _pct(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


class _LockedCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage() + (str(record.exc_info[1]) if record.exc_info and record.exc_info[1] else "")
        if "database is locked" in text:
            self.count += 1


async def run_inprocess(args: argparse.Namespace) -> int:
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("AATM_SANDBOX_MODE", "process")
    tmp = Path(tempfile.mkdtemp(prefix="aatm-load-"))

    from app.core.config import get_settings
    from app.core.security import create_access_token
    from app.db.base import Base
    from app.db.seed import seed_initial_data
    from app.db.session import _apply_sqlite_pragmas, get_db
    from app.main import app
    from app.models import Agent, AIProvider, ApprovalRequest, Project, Task, User
    from app.orchestration.scheduler import TaskScheduler
    from app.providers.base import ProviderResponse
    from app.queue import reset_wake_bus
    from app.queue.base import InProcessWakeBus
    from app.runtime.agent_loop import AgentLoop
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import event, func, select, update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    locked = _LockedCounter()
    logging.getLogger().addHandler(locked)
    logging.getLogger().setLevel(logging.WARNING)

    url = args.database_url or f"sqlite+aiosqlite:///{tmp / 'load.db'}"
    engine = create_async_engine(url, **({"connect_args": {"timeout": 30}} if url.startswith("sqlite") else {"pool_size": 20}))
    if url.startswith("sqlite"):
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    settings = get_settings()
    settings.WORKSPACES_DIR = tmp / "workspaces"
    settings.AUTO_SCALE_MODE = "approval"
    settings.SCALE_UP_WAIT_SECONDS = 0.0
    settings.MAX_CONCURRENT_WORKERS = args.max_workers
    settings.SCALE_UP_STEP = args.scale_step
    reset_wake_bus(InProcessWakeBus())
    os.environ["AATM_LOAD_TEST_DUMMY_API_KEY"] = "load-test-never-sent"

    calls: list[dict] = []

    class ScriptedRouter:
        async def route_and_execute(self, db, request, preferred_provider_id=None):
            start = time.monotonic()
            await asyncio.sleep(args.latency)
            calls.append({"task_id": request.task_id, "start": start, "end": time.monotonic()})
            return ProviderResponse(
                success=True, provider_id="load-test", model="scripted", content=json.dumps({"action": "done", "summary": "ok"}),
                input_tokens=1, output_tokens=1, estimated_cost_usd=0.0, latency_ms=args.latency * 1000,
            )

    async with factory() as db:
        await seed_initial_data(db)
        prov = await db.get(AIProvider, "anthropic-claude")
        prov.is_enabled = True
        prov.credential_ref = "env:AATM_LOAD_TEST_DUMMY_API_KEY"
        await db.execute(update(Task).values(status="completed"))
        await db.execute(update(Agent).values(status="idle", current_task_id=None, provider_id="anthropic-claude"))
        (tmp / "plain").mkdir()
        project = Project(name="Load test", slug="load-test", repo_path=str(tmp / "plain"), default_branch="main", config={})
        db.add(project)
        await db.flush()
        tasks = [Task(project_id=project.id, title=f"Load task {i}", description="x", status="queued", execution_mode="automatic")
                 for i in range(args.tasks)]
        db.add_all(tasks)
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        admin.must_change_password = False
        await db.commit()
        ids = [t.id for t in tasks]
        headers = {"Authorization": f"Bearer {create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)}"}

    async def _db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    router = ScriptedRouter()
    scheduler = TaskScheduler(provider_router=router)
    loop = AgentLoop(session_factory=factory, provider_router=router, poll_seconds=0.02, max_parallel=args.max_workers)
    runner = asyncio.create_task(loop.run_forever())
    latencies: list[float] = []
    approvals = 0
    started = time.monotonic()
    done = 0
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://load") as client:
            while time.monotonic() - started < args.timeout:
                async with factory() as db:
                    await scheduler.run_orchestration_cycle(db)
                    pending = list((await db.execute(select(ApprovalRequest).where(
                        ApprovalRequest.action_type == "scale_up", ApprovalRequest.status == "pending"))).scalars())
                for req in pending:
                    print(f"SIMULATED HUMAN APPROVAL: {req.title}")
                    await client.post(f"/api/v1/approvals/{req.id}/decide", headers=headers, json={"decision": "approved"})
                    approvals += 1
                for path in ("/api/v1/agents", "/api/health"):
                    t0 = time.monotonic()
                    await client.get(path, headers=headers)
                    latencies.append((time.monotonic() - t0) * 1000)
                async with factory() as db:
                    done = (await db.execute(select(func.count()).select_from(Task).where(Task.id.in_(ids), Task.status == "completed"))).scalar()
                if done == len(ids):
                    break
                await asyncio.sleep(0.05)
    finally:
        await loop.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        app.dependency_overrides.pop(get_db, None)
        async with factory() as db:
            workers = (await db.execute(select(func.count()).select_from(Agent).where(Agent.role == "worker", Agent.is_enabled.is_(True)))).scalar()
        await engine.dispose()

    elapsed = time.monotonic() - started
    executed = [c["task_id"] for c in calls if c["task_id"] in set(ids)]
    duplicates = len(executed) - len(set(executed))
    spans = [(c["start"], c["end"]) for c in calls]
    peak = max((sum(1 for s, e in spans if s <= t < e) for t, _ in spans), default=0)
    p95 = _pct(latencies, 95)
    report = {
        "database": url.split("://")[0],
        "tasks": len(ids),
        "completed": done,
        "duplicate_executions": duplicates,
        "database_locked_errors": locked.count,
        "workers_at_end": workers,
        "scale_up_approvals": approvals,
        "peak_parallel_model_turns": peak,
        "elapsed_seconds": round(elapsed, 2),
        "throughput_tasks_per_minute": round(done / elapsed * 60, 1) if elapsed else 0,
        "api_latency_ms": {"p50": round(_pct(latencies, 50), 1), "p95": round(p95, 1), "max": round(max(latencies, default=0), 1)},
    }
    print(json.dumps(report, indent=2))
    checks = {
        "all tasks completed": done == len(ids),
        "no duplicate execution": duplicates == 0,
        "no 'database is locked'": locked.count == 0,
        f"API p95 < {args.max_api_ms} ms": p95 < args.max_api_ms,
        f"default team ran in parallel (peak >= {min(4, len(ids))})": peak >= min(4, len(ids)),
    }
    for name, ok in checks.items():
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if approvals:
        print(f"[INFO] {approvals} scale-up approval(s); peak parallel turns {peak} "
              f"({'extras added capacity' if peak > 4 else 'backlog drained before extras started; raise --latency'})")
    return 0 if all(checks.values()) else 1


async def run_http(args: argparse.Namespace) -> int:
    import httpx

    token = os.environ.get("AATM_LOAD_TEST_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    paths = ["/api/health", "/api/v1/agents", "/api/v1/scaling/status", "/api/v1/tasks"]
    latencies: dict[str, list[float]] = {p: [] for p in paths}
    errors = 0

    async with httpx.AsyncClient(base_url=args.url, timeout=10) as client:
        async def worker() -> None:
            nonlocal errors
            for _ in range(args.requests):
                for path in paths:
                    t0 = time.monotonic()
                    try:
                        res = await client.get(path, headers=headers)
                        if res.status_code >= 500:
                            errors += 1
                    except httpx.HTTPError:
                        errors += 1
                    latencies[path].append((time.monotonic() - t0) * 1000)

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))

    all_ms = [v for vs in latencies.values() for v in vs]
    report = {p: {"p50": round(statistics.median(v), 1), "p95": round(_pct(v, 95), 1)} for p, v in latencies.items() if v}
    print(json.dumps({"url": args.url, "requests": len(all_ms), "errors": errors, "latency_ms": report}, indent=2))
    ok = errors == 0 and _pct(all_ms, 95) < args.max_api_ms
    print(f"[{'PASS' if ok else 'FAIL'}] no 5xx and p95 < {args.max_api_ms} ms")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["inprocess", "http"], default="inprocess")
    parser.add_argument("--tasks", type=int, default=20)
    parser.add_argument("--latency", type=float, default=0.2, help="scripted model latency per turn (seconds)")
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--scale-step", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--database-url", default="", help="async SQLAlchemy URL of a THROWAWAY database (tables are dropped)")
    parser.add_argument("--max-api-ms", type=float, default=200)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--requests", type=int, default=25)
    args = parser.parse_args()
    return asyncio.run(run_inprocess(args) if args.mode == "inprocess" else run_http(args))


if __name__ == "__main__":
    raise SystemExit(main())
