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
