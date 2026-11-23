"""E20 + E29 — plan quality: independent tasks by default, refused dependency chains and research gates,
explicit file ownership, and a shared contract file for frontend/backend work.

E20: a plan where more than half of the tasks depend on one task is refused and asked again with an automatic
re-plan note, even when ARENA_MANAGER_AUTO_APPROVE_PLANS is on. A research or documentation task that other
tasks depend on is refused the same way.
E29: a plan with a frontend and a backend task must name one shared contract file in both descriptions, or the
draft carries the reason "no shared contract" (visible to the owner, not only printed).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from test_e8_e9_planning import _job, _plan, _reset, _task, no_dispatch  # noqa: F401 - fixture and helpers
from test_e12_admin_actions import e12  # noqa: F401 - fixture

from app.core.config import get_settings
from app.models import ManagerJob, Task
from app.orchestration.arena_prompts import (
    build_manager_plan_prompt,
    is_research_or_docs_task,
    validate_manager_plan,
)
from app.orchestration.manager_arena import (
    NO_SHARED_CONTRACT_REASON,
    manager_arena_runtime,
    missing_shared_contract,
    plan_draft_reasons,
    plan_warnings,
)

KNOWN = {"worker-1", "worker-2", "worker-3"}
CONTRACT = "docs/API.md"


def _named(title: str, description: str, *, assignee: str = "worker-1", deps: list[int] | None = None) -> dict:
    return {
        "title": title,
        "assignee": assignee,
        "description": description,
        "acceptance_criteria": ["Tests pass"],
        "depends_on": list(deps or []),
    }


def _frontend(contract: str | None = CONTRACT) -> dict:
    text = "Render the reports page from the response shape."
    return _named("Build the reports page", f"{text} Follow {contract}." if contract else text, assignee="worker-2")


def _backend(contract: str | None = CONTRACT) -> dict:
    text = "Implement GET /api/reports."
    return _named("Add the reports API endpoint", f"{text} Own {contract}." if contract else text, assignee="worker-1")


async def _tasks_titled(factory, titles: list[str]) -> list[Task]:
    async with factory() as db:
        return list((await db.execute(select(Task).where(Task.title.in_(titles)))).scalars())


def _auto_approve(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(get_settings(), "ARENA_MANAGER_AUTO_APPROVE_PLANS", value)


# ----------------------------------------------------------------------------- E20: dependency chains


def test_eight_task_plan_where_seven_tasks_wait_on_the_first_is_refused_for_replanning():
    tasks = [_task(0)] + [_task(i, [0]) for i in range(1, 8)]
    plan, error = validate_manager_plan(_plan(tasks), KNOWN)
    assert plan is None
    assert "Automatic re-plan requested" in error
    assert "7 of 8" in error and "task 0" in error


def test_two_task_chain_and_a_chain_of_one_third_are_still_accepted():
    plan, error = validate_manager_plan(_plan([_task(0), _task(1, [0])]), KNOWN)
    assert error == "" and plan is not None
    tasks = [_task(0), _task(1, [0]), _task(2), _task(3), _task(4), _task(5)]
    assert validate_manager_plan(_plan(tasks), KNOWN)[1] == ""


@pytest.mark.asyncio
async def test_chain_refusal_holds_when_auto_approve_is_on(e12, monkeypatch):  # noqa: F811
    _auto_approve(monkeypatch, True)
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Build the reporting module end to end", project_id)
    tasks = [_task(0)] + [_task(i, [0]) for i in range(1, 8)]
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        reply = "```json\n" + json.dumps(_plan(tasks)) + "\n```"
        parsed, error = await manager_arena_runtime._parse(db, job, reply)
    assert parsed is None
    assert "Automatic re-plan requested" in error and "7 of 8" in error


@pytest.mark.asyncio
async def test_chained_plan_handed_to_the_runtime_becomes_a_draft_and_creates_nothing(e12, no_dispatch, monkeypatch):  # noqa: F811
    """A chained plan that is handed to the runtime anyway still never creates tasks on auto-approve."""
    _auto_approve(monkeypatch, True)
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Build the reporting module end to end", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_task(0), _task(1, [0]), _task(2, [0]), _task(3, [0])]),
                                                raw="{}")
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "draft", "the owner must see the chain, auto-approve does not create it"
    assert not await _tasks_titled(factory, [f"Planned task {i}" for i in range(4)])


# ----------------------------------------------------------------------------- E20: research and documentation gates


def test_research_or_documentation_prerequisite_is_refused_with_a_clear_reason():
    tasks = [
        _named("Research the reporting API options", "Compare the export formats and write up the result."),
        _named("Build the CSV export", "Implement the CSV export using the research result.", deps=[0]),
        _task(2), _task(3), _task(4),
    ]
    plan, error = validate_manager_plan(_plan(tasks), KNOWN)
    assert plan is None
    assert "research or documentation task" in error
    assert "task 0" in error and "must not gate other work" in error


def test_documentation_task_that_others_depend_on_is_refused():
    tasks = [
        _named("Document the export module", "Write the module docs."),
        _named("Implement the export module", "Build the module.", deps=[0]),
        _task(2), _task(3), _task(4),
    ]
    assert "research or documentation task" in validate_manager_plan(_plan(tasks), KNOWN)[1]


def test_research_or_documentation_task_with_no_dependents_is_accepted():
    tasks = [
        _named("Research the reporting API options", "Compare the export formats."),
        _task(1), _task(2), _task(3),
    ]
    assert validate_manager_plan(_plan(tasks), KNOWN)[1] == ""


@pytest.mark.parametrize("title", ["Research competitor exports", "Write documentation for the API",
                                   "Document the export module", "ابحث عن أفضل مكتبة للتصدير", "توثيق وحدة التقارير"])
def test_research_and_documentation_titles_are_recognised(title):
    assert is_research_or_docs_task({"title": title})


@pytest.mark.parametrize("title", ["Build the document upload form", "Implement search filters", "Add the invoice export"])
def test_ordinary_implementation_titles_are_not_research(title):
    assert not is_research_or_docs_task({"title": title})


# ----------------------------------------------------------------------------- independent plans are accepted


def test_independent_plan_is_accepted_without_warnings_or_reasons():
    tasks = [_task(i) for i in range(4)]
    plan, error = validate_manager_plan(_plan(tasks), KNOWN)
    assert error == "" and plan is not None
    assert plan_warnings(plan["tasks"]) == []
    assert plan_draft_reasons(plan["tasks"], "Build the reporting module.\nExports and dashboards are separate parts.", False) == []


@pytest.mark.asyncio
async def test_independent_plan_creates_its_tasks_with_no_warnings(e12, no_dispatch):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Write the three helper modules\nEach one stands alone.", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_task(i) for i in range(3)]), raw="{}")
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "done"
        assert job.result["warnings"] == []
    assert len(await _tasks_titled(factory, [f"Planned task {i}" for i in range(3)])) == 3


# ----------------------------------------------------------------------------- E29: shared contract


def test_frontend_and_backend_without_a_shared_contract_give_the_no_shared_contract_reason():
    tasks = [_frontend(contract=None), _backend(contract=None)]
    reasons = plan_draft_reasons(tasks, "Add a reports page backed by an API", False)
    assert any(r.startswith("no shared contract") for r in reasons)
    assert NO_SHARED_CONTRACT_REASON in reasons


def test_a_contract_named_in_both_descriptions_removes_the_reason():
    tasks = [_frontend(), _backend()]
    assert missing_shared_contract(tasks) is False
    assert plan_draft_reasons(tasks, "Add a reports page backed by an API", False) == []


def test_a_contract_named_in_only_one_description_is_not_shared():
    tasks = [_frontend(contract=None), _backend()]
    assert missing_shared_contract(tasks) is True
    assert any("no shared contract" in r for r in plan_draft_reasons(tasks, "Reports page and API", False))


def test_different_contract_files_are_not_shared():
    tasks = [_named("Build the reports page", "Follow docs/UI.md.", assignee="worker-2"),
             _named("Add the reports API endpoint", "Own docs/API.md.")]
    assert missing_shared_contract(tasks) is True


def test_no_shared_contract_rule_only_applies_when_both_sides_are_present():
    assert missing_shared_contract([_frontend(contract=None), _task(1)]) is False
    assert missing_shared_contract([_backend(contract=None), _task(1)]) is False


@pytest.mark.asyncio
async def test_frontend_backend_plan_without_contract_becomes_a_draft_even_on_auto_approve(e12, no_dispatch, monkeypatch):  # noqa: F811
    _auto_approve(monkeypatch, True)
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Add a reports page backed by an API", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_frontend(contract=None), _backend(contract=None)]),
                                                raw="{}")
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "draft"
        assert any("no shared contract" in r for r in job.result["draft_reasons"])
    assert await _tasks_titled(factory, ["Build the reports page", "Add the reports API endpoint"]) == []


@pytest.mark.asyncio
async def test_frontend_backend_plan_with_a_shared_contract_is_created(e12, no_dispatch):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Add a reports page backed by an API\nThe contract is shared.", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_frontend(), _backend()]), raw="{}")
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "done"
    assert len(await _tasks_titled(factory, ["Build the reports page", "Add the reports API endpoint"])) == 2


# ----------------------------------------------------------------------------- the planning prompt carries the rules


def test_plan_prompt_states_independence_ownership_and_the_shared_contract_rule():
    prompt = build_manager_plan_prompt("Build the reports", None, [], [])
    for needle in (
        "## Plan quality rules (mandatory)",
        "Independence is the default",
        "must never gate other work",
        "research or documentation task",
        "Ownership:",
        "Two tasks must never change the same function",
        "one shared contract file",
        "docs/API.md",
        "must both name that exact file",
    ):
        assert needle in prompt, needle
    rules = prompt.index("## Plan quality rules")
    assert prompt.index("## B. EXISTING TASKS") < rules < prompt.index("## What to reply")
