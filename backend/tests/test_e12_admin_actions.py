"""E12 — admin actions that change state in bulk or free a worker: chat clear (archive), chat paging,
manager job cancel and Arena session close.

Every endpoint: admin only, writes an audit entry that records the backup path, takes a backup first,
and leaves the worker free.
"""

from __future__ import annotations

import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_user  # noqa: F401 - imported so the override path is loaded
from app.core.security import create_access_token
from app.db import backup as backup_module
from app.db.base import Base, utcnow
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import Agent, ArenaSession, AuditLog, ChatMessage, ManagerJob, Project, Task, User


@pytest.fixture()
async def e12(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "e12.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await seed_initial_data(db)
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        admin.must_change_password = False
        db.add(User(email="operator@example.com", full_name="Operator", password_hash="x", role="operator",
                    must_change_password=False))
        await db.commit()
    # Backups go next to the test database (tmp_path/backups), never into the real data directory.
    monkeypatch.setattr(backup_module, "_default_source", lambda: db_file)

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    yield factory, tmp_path
    app.dependency_overrides.pop(get_db, None)
    await engine.dispose()


async def _headers(factory, role: str) -> dict[str, str]:
    async with factory() as db:
        user = (await db.execute(select(User).where(User.role == role))).scalars().first()
        token = create_access_token(subject=user.email, role=user.role, token_version=user.token_version)
        return {"Authorization": f"Bearer {token}"}


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _assert_backup(path_str: str, tmp_path: Path) -> None:
    path = Path(path_str)
    assert path.exists(), path
    assert path.parent == tmp_path / "backups"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


async def _audit(factory, action: str) -> list[AuditLog]:
    async with factory() as db:
        return list((await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


# ----------------------------------------------------------------------------- chat clear + paging


async def _seed_chat(factory, n: int) -> list[str]:
    ids = []
    async with factory() as db:
        await db.execute(ChatMessage.__table__.delete())  # drop the seeded welcome message
        base = utcnow()
        for i in range(n):
            msg = ChatMessage(project_id=None, sender_type="user", sender_id="admin", content=f"m{i}",
                              created_at=base + timedelta(seconds=i))
            db.add(msg)
            await db.flush()
            ids.append(msg.id)
        await db.commit()
    return ids


async def test_chat_clear_requires_auth_and_admin(e12):
    factory, _ = e12
    await _seed_chat(factory, 1)
    async with await _client() as client:
        anon = await client.delete("/api/v1/chat")
        assert anon.status_code == 401
        op = await client.delete("/api/v1/chat", headers=await _headers(factory, "operator"))
        assert op.status_code == 403
    async with factory() as db:
        assert len((await db.execute(select(ChatMessage))).scalars().all()) == 1


async def test_chat_clear_archives_backs_up_and_audits_without_touching_audit_log(e12):
    factory, tmp_path = e12
    await _seed_chat(factory, 3)
    prior_audit = {a.id for a in (await _audit_rows(factory))}
    async with await _client() as client:
        res = await client.delete("/api/v1/chat", headers=await _headers(factory, "admin"))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["archived"] == 3
        listed = await client.get("/api/v1/chat", headers=await _headers(factory, "admin"))
        assert listed.json() == []

    entries = await _audit(factory, "chat.clear")
    assert len(entries) == 1
    _assert_backup(entries[0].details["backup_path"], tmp_path)
    assert entries[0].details["backup_path"] == body["backup_path"]

    # Archived, not deleted; and the audit log is untouched by the clear.
    async with factory() as db:
        rows = list((await db.execute(select(ChatMessage))).scalars())
        assert len(rows) == 3 and all(r.archived_at is not None for r in rows)
    assert prior_audit <= {a.id for a in (await _audit_rows(factory))}


async def _audit_rows(factory) -> list[AuditLog]:
    async with factory() as db:
        return list((await db.execute(select(AuditLog))).scalars())


async def test_chat_list_pages_newest_first_then_older(e12):
    factory, _ = e12
    await _seed_chat(factory, 5)
    async with await _client() as client:
        headers = await _headers(factory, "admin")
        first = (await client.get("/api/v1/chat?limit=2", headers=headers)).json()
        assert [m["content"] for m in first] == ["m3", "m4"]  # newest two, oldest first
        second = (await client.get(f"/api/v1/chat?limit=2&before={first[0]['id']}", headers=headers)).json()
        assert [m["content"] for m in second] == ["m1", "m2"]
        unbounded = (await client.get("/api/v1/chat", headers=headers)).json()
        assert len(unbounded) == 5


# ----------------------------------------------------------------------------- manager job cancel


async def test_manager_job_cancel_frees_manager_backs_up_and_audits(e12):
    factory, tmp_path = e12
    async with factory() as db:
        job = ManagerJob(kind="plan", status="needs_human", request_text="goal", context={})
        db.add(job)
        manager = await db.get(Agent, "manager-0")
        manager.status = "needs_human"
        await db.commit()
        job_id = job.id

    async with await _client() as client:
        anon = await client.post(f"/api/v1/arena/manager/jobs/{job_id}/cancel")
        assert anon.status_code == 401
        op = await client.post(f"/api/v1/arena/manager/jobs/{job_id}/cancel", headers=await _headers(factory, "operator"))
        assert op.status_code == 403
        res = await client.post(f"/api/v1/arena/manager/jobs/{job_id}/cancel", headers=await _headers(factory, "admin"))
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "failed"

    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "failed" and job.finished_at is not None
        assert (await db.get(Agent, "manager-0")).status == "idle"
    entries = await _audit(factory, "manager_job.cancel")
    assert len(entries) == 1
    _assert_backup(entries[0].details["backup_path"], tmp_path)


async def test_manager_job_cancel_refuses_a_running_job(e12):
    factory, _ = e12
    async with factory() as db:
        job = ManagerJob(kind="plan", status="running", request_text="goal", context={})
        db.add(job)
        await db.commit()
        job_id = job.id
    async with await _client() as client:
        res = await client.post(f"/api/v1/arena/manager/jobs/{job_id}/cancel", headers=await _headers(factory, "admin"))
    assert res.status_code == 409


# ----------------------------------------------------------------------------- session close


async def _open_worker_session(factory) -> tuple[str, str]:
    async with factory() as db:
        worker = await db.get(Agent, "worker-1")
        project = (await db.execute(select(Project))).scalars().first()
        task = Task(project_id=project.id, division_code="D01", title="t", description="d", status="in_progress",
                    assigned_agent_id=worker.id, execution_mode="manual")
        db.add(task)
        await db.flush()
        session = ArenaSession(agent_id=worker.id, profile_id=worker.browser_profile_id or "profile-worker-1",
                               task_id=task.id, active_task_id=task.id, phase="working", is_open=True, events=[])
        db.add(session)
        worker.status = "working"
        worker.current_task_id = task.id
        await db.commit()
        return session.id, task.id


async def test_session_close_frees_the_worker_backs_up_and_audits(e12):
    factory, tmp_path = e12
    session_id, task_id = await _open_worker_session(factory)

    async with await _client() as client:
        op = await client.post(f"/api/v1/arena/sessions/{session_id}/close", headers=await _headers(factory, "operator"))
        assert op.status_code == 403
        res = await client.post(f"/api/v1/arena/sessions/{session_id}/close", headers=await _headers(factory, "admin"))
        assert res.status_code == 200, res.text
        again = await client.post(f"/api/v1/arena/sessions/{session_id}/close", headers=await _headers(factory, "admin"))
        assert again.status_code == 409

    async with factory() as db:
        session = await db.get(ArenaSession, session_id)
        assert session.is_open is False and session.phase == "closed"
        worker = await db.get(Agent, "worker-1")
        assert worker.status == "idle" and worker.current_task_id is None
        task = await db.get(Task, task_id)
        assert task.status == "blocked" and task.lock_owner is None
    entries = await _audit(factory, "arena.session_close")
    assert len(entries) == 1
    _assert_backup(entries[0].details["backup_path"], tmp_path)
