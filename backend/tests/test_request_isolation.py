"""Request isolation: a new request is planned alone; other projects' tasks never enter its planning context."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures

from app.models import Project, Task
from app.orchestration.arena_prompts import build_manager_plan_prompt
from app.orchestration.manager_arena import ManagerArenaRuntime as ManagerArena


def _plan_prompt(goal: str, tasks: list[dict]) -> str:
    project = SimpleNamespace(name="Game Project", config={}, repo_path="/tmp/none", default_branch="main", slug="game", description="", repo_url="")
    return build_manager_plan_prompt(goal, project, [], tasks)


def test_prompt_separates_current_request_historical_tasks_and_project_context():
    prompt = _plan_prompt("Write a fun lightweight game", [
        {"title": "OSINT phone search", "status": "queued", "assignee": "worker-3"},
    ])
    active = prompt.index("## A. CURRENT REQUEST")
    readonly = prompt.index("## B. EXISTING TASKS")
    reference = prompt.index("## C. PROJECT CONTEXT")
    assert active < readonly < reference
    # The current goal is only in section A; the historical task is only in section B.
    assert prompt.index("Write a fun lightweight game") > active
    assert active < prompt.index("Write a fun lightweight game") < readonly
    assert prompt.index("OSINT phone search") > readonly
    assert "READ-ONLY" in prompt and "REFERENCE ONLY" in prompt


def test_prompt_never_presents_a_historical_task_as_an_objective():
    prompt = _plan_prompt("Build a calculator", [{"title": "Sports app API", "status": "blocked", "assignee": None}])
    objective = prompt[prompt.index("## A. CURRENT REQUEST"):prompt.index("## B. EXISTING TASKS")]
    assert "Sports app API" not in objective


@pytest.mark.asyncio
async def test_open_tasks_without_a_project_returns_nothing(env):  # noqa: F811
    async with env() as db:
        project = Project(name="Only", slug="only", repo_path="/tmp/none", default_branch="main")
        db.add(project)
        await db.flush()
        db.add(Task(project_id=project.id, title="Other project work", description="x", status="queued",
                    task_type="implementation", execution_mode="manual"))
        await db.commit()
    async with env() as db:
        assert await ManagerArena()._open_tasks(db, None) == []


@pytest.mark.asyncio
async def test_open_tasks_are_limited_to_their_own_project(env):  # noqa: F811
    async with env() as db:
        a = Project(name="A", slug="proj-a", repo_path="/tmp/a", default_branch="main")
        b = Project(name="B", slug="proj-b", repo_path="/tmp/b", default_branch="main")
        db.add_all([a, b])
        await db.flush()
        db.add(Task(project_id=a.id, title="A task", description="x", status="queued", task_type="implementation", execution_mode="manual"))
        db.add(Task(project_id=b.id, title="B task", description="x", status="queued", task_type="implementation", execution_mode="manual"))
        await db.commit()
        a_id = a.id
    async with env() as db:
        titles = [t["title"] for t in await ManagerArena()._open_tasks(db, a_id)]
        assert titles == ["A task"]


# ----------------------------------------------------------------------------- lifecycle: retry, concurrency, dispatch
# These run against the isolated e12 database (tmp_path); the live database and live workers are never touched.

import asyncio  # noqa: E402

from sqlalchemy import select, update  # noqa: E402
from test_e12_admin_actions import e12  # noqa: E402, F401 - fixture

from app.models import Agent, ManagerJob  # noqa: E402
from app.orchestration import arena_runner as arena_runner_module  # noqa: E402
from app.orchestration.manager_arena import manager_arena_runtime  # noqa: E402
from app.orchestration.scheduler import task_scheduler  # noqa: E402


def _plan(prefix: str, n: int = 2) -> dict:
    return {"summary_ar": "خطة", "tasks": [
        {"title": f"{prefix} {i}", "assignee": "worker-1", "description": f"{prefix} part {i}",
         "acceptance_criteria": ["Tests pass"], "depends_on": []} for i in range(n)]}


async def _quiet(factory) -> str:
    async with factory() as db:
        await db.execute(update(Task).values(status="completed"))
        await db.execute(update(Agent).values(status="idle", current_task_id=None))
        project = (await db.execute(select(Project))).scalars().first()
        await db.commit()
        return project.id


async def _second_project(factory) -> str:
    async with factory() as db:
        other = Project(name="Other", slug="other-proj", repo_path="/tmp/other", default_branch="main")
        db.add(other)
        await db.commit()
        return other.id


async def _job(factory, goal: str, project_id: str, status: str = "running") -> str:
    async with factory() as db:
        job = ManagerJob(kind="plan", project_id=project_id, request_text=goal, status=status, context={})
        db.add(job)
        await db.commit()
        return job.id


@pytest.fixture()
def dispatch_spy(monkeypatch):
    calls: list[dict] = []

    async def _cycle(db, **kwargs):
        calls.append(kwargs)
        return {"dispatched": []}

    monkeypatch.setattr(task_scheduler, "run_orchestration_cycle", _cycle)
    return calls


@pytest.mark.asyncio
async def test_sequential_requests_get_their_own_request_id_and_tasks(e12, dispatch_spy):  # noqa: F811
    factory, _ = e12
    project_id = await _quiet(factory)
    first, second = await _job(factory, "Build a game", project_id), await _job(factory, "Add a scoreboard", project_id)
    assert first != second
    for job_id, prefix in ((first, "Game"), (second, "Score")):
        async with factory() as db:
            await manager_arena_runtime._apply_plan(db, await db.get(ManagerJob, job_id), _plan(prefix), raw="{}")
    async with factory() as db:
        a = await manager_arena_runtime.tasks_for_request(db, await db.get(ManagerJob, first))
        b = await manager_arena_runtime.tasks_for_request(db, await db.get(ManagerJob, second))
    assert [t.title for t in a] == ["Game 0", "Game 1"] and [t.title for t in b] == ["Score 0", "Score 1"]
    assert all(t.checkpoints[0]["request_id"] == first for t in a)
    # The second request's prompt sees the first request's tasks only as read-only history.
    async with factory() as db:
        job = await db.get(ManagerJob, second)
        job.kind = "plan"
        prompt = await manager_arena_runtime._prompt(db, job)
    objective = prompt[prompt.index("## A. CURRENT REQUEST"):prompt.index("## B. EXISTING TASKS")]
    history = prompt[prompt.index("## B. EXISTING TASKS"):prompt.index("## C. PROJECT CONTEXT")]
    assert "Add a scoreboard" in objective and "Game 0" not in objective and "Game 0" in history


@pytest.mark.asyncio
async def test_concurrent_requests_in_two_projects_stay_isolated(e12, dispatch_spy):  # noqa: F811
    factory, _ = e12
    a_id = await _quiet(factory)
    b_id = await _second_project(factory)
    ja, jb = await _job(factory, "OSINT phone search", a_id), await _job(factory, "Fun lightweight game", b_id)

    async def run(job_id, prefix):
        async with factory() as db:
            await manager_arena_runtime._apply_plan(db, await db.get(ManagerJob, job_id), _plan(prefix), raw="{}")

    await asyncio.gather(run(ja, "Osint"), run(jb, "Game"))
    async with factory() as db:
        a_tasks = await manager_arena_runtime.tasks_for_request(db, await db.get(ManagerJob, ja))
        b_tasks = await manager_arena_runtime.tasks_for_request(db, await db.get(ManagerJob, jb))
        assert {t.project_id for t in a_tasks} == {a_id} and {t.project_id for t in b_tasks} == {b_id}
        assert all(t.title.startswith("Osint") for t in a_tasks) and all(t.title.startswith("Game") for t in b_tasks)
        prompts = await asyncio.gather(*[manager_arena_runtime._prompt(db, await db.get(ManagerJob, j)) for j in (ja, jb)])
    assert "OSINT phone search" not in prompts[1] and "Osint 0" not in prompts[1]
    assert "Fun lightweight game" not in prompts[0] and "Game 0" not in prompts[0]


@pytest.mark.asyncio
async def test_retrying_the_same_request_creates_no_duplicates_and_no_second_dispatch(e12, dispatch_spy):  # noqa: F811
    factory, _ = e12
    project_id = await _quiet(factory)
    job_id = await _job(factory, "Build a game", project_id)
    for _ in range(3):
        async with factory() as db:
            job = await db.get(ManagerJob, job_id)
            await manager_arena_runtime._create_plan_tasks(db, job, _plan("Game"), raw="{}", warnings=[])
    # Concurrent retries of the same request are serialized and still create one set of tasks.
    async def retry():
        async with factory() as db:
            await manager_arena_runtime._create_plan_tasks(db, await db.get(ManagerJob, job_id), _plan("Game"), raw="{}", warnings=[])
    await asyncio.gather(retry(), retry())
    async with factory() as db:
        titles = [t.title for t in (await db.execute(select(Task).where(Task.title.like("Game %")))).scalars()]
    assert sorted(titles) == ["Game 0", "Game 1"]
    assert len(dispatch_spy) == 1


@pytest.mark.asyncio
async def test_restart_retry_of_a_planned_request_does_not_prompt_the_manager_again(e12, dispatch_spy, monkeypatch):  # noqa: F811
    factory, _ = e12
    project_id = await _quiet(factory)
    job_id = await _job(factory, "Build a game", project_id)
    async with factory() as db:
        await manager_arena_runtime._insert_plan_tasks(db, await db.get(ManagerJob, job_id), _plan("Game"))
    # Crash before the job was finished: restart puts it back in the queue.

    async def ready(_profile):
        return {"connected": True, "code": "ready", "reason_ar": ""}

    async def no_session(*_a, **_k):
        return SimpleNamespace(id="s1", events=[])

    async def must_not_prompt(*_a, **_k):
        raise AssertionError("the manager was prompted again for an already planned request")

    monkeypatch.setattr("app.orchestration.manager_arena.manager_readiness", ready)
    monkeypatch.setattr(manager_arena_runtime, "_ensure_session", no_session)
    monkeypatch.setattr(manager_arena_runtime, "_exchange", must_not_prompt)
    async with factory() as db:
        result = await manager_arena_runtime._run_job(db, await db.get(ManagerJob, job_id))
    assert result["status"] == "done" and result["idempotent_retry"]
    async with factory() as db:
        assert len(list((await db.execute(select(Task).where(Task.title.like("Game %")))).scalars())) == 2


@pytest.mark.asyncio
async def test_draft_cannot_be_approved_twice(e12, dispatch_spy):  # noqa: F811
    factory, _ = e12
    project_id = await _quiet(factory)
    job_id = await _job(factory, "Build a game", project_id, status="running")
    async with factory() as db:
        await manager_arena_runtime._apply_plan(db, await db.get(ManagerJob, job_id), _plan("Game", 7), raw="{}")
        assert (await db.get(ManagerJob, job_id)).status == "draft"

    async def approve():
        async with factory() as db:
            try:
                return await manager_arena_runtime.approve_draft(db, await db.get(ManagerJob, job_id))
            except ValueError:
                return None

    results = await asyncio.gather(approve(), approve())
    assert sum(r is not None for r in results) == 1
    async with factory() as db:
        assert len(list((await db.execute(select(Task).where(Task.title.like("Game %")))).scalars())) == 7


@pytest.mark.asyncio
async def test_new_plan_preserves_existing_statuses_assignments_and_dispatches_only_its_own_tasks(e12, dispatch_spy):  # noqa: F811
    factory, _ = e12
    project_id = await _quiet(factory)
    before: dict[str, tuple[str, str | None]] = {}
    async with factory() as db:
        for status in ("queued", "blocked", "paused", "completed", "in_progress", "failed", "cancelled", "awaiting_approval"):
            task = Task(project_id=project_id, division_code="D01", title=f"Old {status}", description="old work",
                        status=status, assigned_agent_id="worker-2", execution_mode="manual", dependencies=[])
            db.add(task)
        await db.commit()
        for t in (await db.execute(select(Task).where(Task.title.like("Old %")))).scalars():
            before[t.id] = (t.status, t.assigned_agent_id)
        agents_before = {a.id: (a.status, a.current_task_id) for a in (await db.execute(select(Agent))).scalars()}
    job_id = await _job(factory, "Build a game", project_id)
    async with factory() as db:
        await manager_arena_runtime._apply_plan(db, await db.get(ManagerJob, job_id), _plan("Game"), raw="{}")
    async with factory() as db:
        after = {t.id: (t.status, t.assigned_agent_id) for t in (await db.execute(select(Task).where(Task.title.like("Old %")))).scalars()}
        new_ids = {t.id for t in await manager_arena_runtime.tasks_for_request(db, await db.get(ManagerJob, job_id))}
        agents_after = {a.id: (a.status, a.current_task_id) for a in (await db.execute(select(Agent))).scalars()}
    assert after == before
    assert agents_after == agents_before
    assert dispatch_spy == [{"only_task_ids": new_ids}]


@pytest.mark.asyncio
async def test_scheduler_cycle_limited_to_new_tasks_never_dispatches_old_queued_work(e12, monkeypatch):  # noqa: F811
    factory, _ = e12
    project_id = await _quiet(factory)

    async def nothing(*_a, **_k):
        return {}

    async def none_disconnected(_workers):
        return set()

    # Keep the cycle away from the bridge and Arena: only the local assignment logic runs.
    monkeypatch.setattr("app.browser.arena_sessions.sync_profiles_with_bridge", nothing)
    monkeypatch.setattr(arena_runner_module.arena_runner, "tick", nothing)
    monkeypatch.setattr(task_scheduler, "_arena_disconnected_agent_ids", none_disconnected)
    async with factory() as db:
        old = Task(project_id=project_id, division_code="D01", title="Old calculator", description="old",
                   priority="high", status="queued", execution_mode="auto", dependencies=[])
        new = Task(project_id=project_id, division_code="D01", title="New game", description="new",
                   priority="low", status="queued", execution_mode="auto", dependencies=[])
        db.add_all([old, new])
        await db.execute(update(Agent).where(Agent.role == "worker").values(is_enabled=True, is_emergency_stopped=False,
                                                                             status="idle", current_task_id=None))
        await db.commit()
        old_id, new_id = old.id, new.id
    async with factory() as db:
        result = await task_scheduler.run_orchestration_cycle(db, only_task_ids={new_id})
    assert [d["title"] for d in result["dispatched"]] == ["New game"]
    async with factory() as db:
        assert (await db.get(Task, old_id)).status == "queued"
