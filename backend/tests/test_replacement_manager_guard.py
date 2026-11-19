"""E2 regression: the manager is never a replacement and a refusal explains itself (EN/AR/FA).

The manager plans and coordinates; it must never take over implementation work. When no free
worker can take over, the request is refused with a reason (409) instead of falling back to the
manager. Moving the tasks the manager holds back to the queue is an explicit operator command.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.git_workflows.manager import git_workflow_manager
from app.main import app
from app.models import Agent, Project, Task, User
from app.orchestration.replacement import NoFreeWorkerError, replacement_service


@pytest.fixture()
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'replacement-guard.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await seed_initial_data(db)

    async def override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override
    yield sessions
    app.dependency_overrides.pop(get_db, None)
    await engine.dispose()


async def _admin_headers(sessions) -> dict[str, str]:
    async with sessions() as db:
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        assert admin is not None
        admin.must_change_password = False
        await db.commit()
        token = create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)
        return {"Authorization": f"Bearer {token}"}


def _no_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        git_workflow_manager,
        "remove_agent_worktree",
        lambda agent_id, *args, **kwargs: {"ok": True, "removed": True, "agent_id": agent_id},
    )


async def _busy_every_worker(db) -> None:
    """Every worker is busy: nobody is free to take over."""
    workers = (await db.execute(select(Agent).where(Agent.role == "worker"))).scalars().all()
    assert workers
    for worker in workers:
        worker.status = "working"
    await db.commit()


@pytest.mark.asyncio
async def test_replacement_never_selects_the_manager(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The manager matches the failing employee's skills best, but it must never be the replacement."""
    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")
    _no_worktree(monkeypatch)

    async with env() as db:
        manager = await db.get(Agent, "manager-0")
        assert manager is not None and manager.status == "idle" and manager.role == "manager"

        failing = Agent(
            id="emp-fail-e2",
            name="Failing E2 Employee",
            role="worker",
            specialty="Backend",
            # Same skills/divisions as the manager: the manager used to win the ranking.
            skills=list(manager.skills or []),
            divisions=list(manager.divisions or []),
            status="error",
            is_enabled=True,
            workspace_path=str(tmp_path / "workspaces" / "emp-fail-e2"),
        )
        db.add(failing)
        await db.commit()

        result = await replacement_service.replace_employee(db, failing.id, reason="Repeated timeout", actor="manager-0")

        assert result["replacement_employee"] != "manager-0"
        replacement = await db.get(Agent, result["replacement_employee"])
        assert replacement is not None
        assert replacement.role != "manager"
        assert replacement.status == "idle"


@pytest.mark.asyncio
async def test_replacement_is_refused_when_no_worker_is_free(env) -> None:
    """No free worker: the service refuses instead of falling back to the manager."""
    async with env() as db:
        await _busy_every_worker(db)
        with pytest.raises(NoFreeWorkerError) as excinfo:
            await replacement_service.replace_employee(db, "worker-1", reason="Repeated timeout", actor="manager-0")
        assert excinfo.value.failing_agent_id == "worker-1"
        assert set(excinfo.value.messages) == {"en", "ar", "fa"}

        # Nothing changed: the failing employee is not marked offline and no task was transferred.
        failing = await db.get(Agent, "worker-1")
        assert failing is not None and failing.status == "working"


@pytest.mark.asyncio
async def test_replace_endpoint_returns_409_when_no_worker_is_free(env) -> None:
    """The API answers 409 with the reason in English, Arabic and Persian."""
    headers = await _admin_headers(env)
    async with env() as db:
        await _busy_every_worker(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/employees/worker-1/replace", headers=headers)

    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "no_free_worker"
    assert set(body["messages"]) == {"en", "ar", "fa"}
    assert "worker" in body["messages"]["en"].lower()
    assert body["detail"]
    for language in ("en", "ar", "fa"):
        assert body["messages"][language] in body["detail"]


@pytest.mark.asyncio
async def test_requeue_manager_tasks_moves_the_tasks_back_to_the_queue(env) -> None:
    """The operator command moves the manager's tasks back to `queued` and reports the count."""
    headers = await _admin_headers(env)
    async with env() as db:
        project = (await db.execute(select(Project))).scalars().first()
        manager_task = Task(project_id=project.id, title="Manager owned task", status="in_progress",
                            assigned_agent_id="manager-0")
        worker_task = Task(project_id=project.id, title="Worker owned task", status="queued",
                           assigned_agent_id="worker-1")
        db.add_all([manager_task, worker_task])
        await db.commit()
        manager_task_id = manager_task.id
        worker_task_id = worker_task.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/employees/requeue-manager-tasks", headers=headers)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["agent_id"] == "manager-0"
    assert body["requeued"] >= 1
    assert manager_task_id in body["task_ids"]
    assert worker_task_id not in body["task_ids"]

    async with env() as db:
        requeued = await db.get(Task, manager_task_id)
        assert requeued is not None
        assert requeued.status == "queued"
        assert requeued.assigned_agent_id is None
        untouched = await db.get(Task, worker_task_id)
        assert untouched is not None
        assert untouched.assigned_agent_id == "worker-1"


def test_requeue_manager_tasks_is_never_run_automatically() -> None:
    """The requeue is an explicit operator command: no automatic path may call it."""
    import app.orchestration.arena_runner
    import app.orchestration.manager_arena
    import app.orchestration.review
    import app.orchestration.scheduler
    import app.qa.engine

    automatic_modules = (
        app.orchestration.scheduler,
        app.orchestration.review,
        app.orchestration.arena_runner,
        app.orchestration.manager_arena,
        app.qa.engine,
    )
    for module in automatic_modules:
        source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
        assert "requeue_manager_tasks" not in source, f"{module.__name__} must not requeue tasks automatically"


@pytest.mark.asyncio
async def test_requeue_manager_tasks_reports_zero_when_the_manager_holds_nothing(env) -> None:
    """The command is safe to run twice: the second run reports nothing left to move."""
    headers = await _admin_headers(env)
    async with env() as db:
        project = (await db.execute(select(Project))).scalars().first()
        db.add(Task(project_id=project.id, title="Manager owned task", status="queued", assigned_agent_id="manager-0"))
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v1/employees/requeue-manager-tasks", headers=headers)
        second = await client.post("/api/v1/employees/requeue-manager-tasks", headers=headers)

    assert first.status_code == 200, first.text
    assert first.json()["requeued"] >= 1
    assert second.status_code == 200, second.text
    assert second.json()["requeued"] == 0


def test_no_free_worker_messages_cover_the_three_languages() -> None:
    """The refusal is readable in English, Arabic and Persian."""
    from app.orchestration.replacement import NO_FREE_WORKER_MESSAGES

    assert set(NO_FREE_WORKER_MESSAGES) == {"en", "ar", "fa"}
    for language, message in NO_FREE_WORKER_MESSAGES.items():
        assert isinstance(message, str) and len(message) > 20, language
    assert any("\u0600" <= ch <= "\u06ff" for ch in NO_FREE_WORKER_MESSAGES["ar"])
    assert any("\u0600" <= ch <= "\u06ff" for ch in NO_FREE_WORKER_MESSAGES["fa"])


@pytest.mark.asyncio
async def test_replace_endpoint_still_reports_unknown_employees_as_400(env) -> None:
    """An unknown employee is a bad request, not a conflict."""
    headers = await _admin_headers(env)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/employees/does-not-exist/replace", headers=headers)
    assert res.status_code == 400, res.text


@pytest.mark.asyncio
async def test_replace_endpoint_still_replaces_when_a_worker_is_free(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The normal path is unchanged: a free worker takes the tasks over."""
    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")
    _no_worktree(monkeypatch)
    headers = await _admin_headers(env)

    async with env() as db:
        project = (await db.execute(select(Project))).scalars().first()
        task = Task(project_id=project.id, title="Task of the failing worker", status="in_progress",
                    assigned_agent_id="worker-1")
        db.add(task)
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/employees/worker-1/replace", headers=headers)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["replaced_employee"] == "worker-1"
    assert body["replacement_employee"] != "manager-0"
    assert body["transferred_tasks"]

    async with env() as db:
        moved = (await db.execute(select(Task).where(Task.assigned_agent_id == body["replacement_employee"]))).scalars().all()
        assert "Task of the failing worker" in [t.title for t in moved]
        # The manager keeps its own tasks: a replacement never takes work away from it.
        manager_tasks = (await db.execute(select(Task).where(Task.assigned_agent_id == "manager-0"))).scalars().all()
        assert all(t.title != "Task of the failing worker" for t in manager_tasks)
