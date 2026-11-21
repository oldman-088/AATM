"""E8 + E9 — manager planning and parallel dispatch.

E8: a plan is validated before any task is created. Cycles and unknown dependencies are rejected; a
chained plan (more than half the tasks depend on one task) is warned about; more than 5 tasks, a
one-line request with more than 3 tasks, or an ambiguous goal becomes a draft with Approve / Edit /
Reject; goals about unauthorized scraping, surveillance or profiling of private people, or CAPTCHA /
anti-bot bypass create NO tasks and return a reason.

E9: every queued task whose dependencies are satisfied is dispatched to every idle worker in one cycle,
respecting the project's concurrency limit.
"""

from __future__ import annotations

import pytest
from app.models import Agent, AuditLog, ManagerJob, Project, Task
from app.orchestration.manager_arena import (
    MAX_TASKS_ONE_LINE,
    MAX_TASKS_WITHOUT_DRAFT,
    manager_arena_runtime,
    plan_draft_reasons,
    plan_warnings,
    policy_violation,
    validate_plan_graph,
)
from app.orchestration.scheduler import task_scheduler
from sqlalchemy import select, update
from test_e12_admin_actions import _client, _headers, e12  # noqa: F401 - fixture + helpers

PLANNED = "Planned task"


def _task(i: int, deps: list[int] | None = None, assignee: str = "worker-1") -> dict:
    return {
        "title": f"{PLANNED} {i}",
        "assignee": assignee,
        "description": f"Do part {i} of the work.",
        "acceptance_criteria": ["Tests pass"],
        "depends_on": list(deps or []),
    }


def _plan(tasks: list[dict], *, ambiguous: bool | None = None) -> dict:
    plan = {"summary_ar": "خطة تجريبية", "tasks": tasks}
    if ambiguous is not None:
        plan["ambiguous"] = ambiguous
    return plan


async def _reset(factory) -> str:
    """Quiet the seeded data so only the test's own tasks can be dispatched; return a project id."""
    async with factory() as db:
        await db.execute(update(Task).values(status="completed"))
        await db.execute(update(Agent).values(status="idle", current_task_id=None))
        project = (await db.execute(select(Project))).scalars().first()
        await db.commit()
        return project.id


async def _job(factory, goal: str, project_id: str) -> str:
    async with factory() as db:
        job = ManagerJob(kind="plan", project_id=project_id, request_text=goal, status="running", context={})
        db.add(job)
        await db.commit()
        return job.id


async def _planned_tasks(factory) -> list[Task]:
    async with factory() as db:
        return list((await db.execute(select(Task).where(Task.title.like(f"{PLANNED}%")))).scalars())


@pytest.fixture()
def no_dispatch(monkeypatch):
    """Plan application ends with an orchestration cycle; these tests check planning, not dispatch."""
    calls: list[int] = []

    async def _cycle(db, **_kwargs):
        calls.append(1)
        return {"dispatched": []}

    monkeypatch.setattr(task_scheduler, "run_orchestration_cycle", _cycle)
    return calls


# ----------------------------------------------------------------------------- E8: pure validators


def test_graph_validator_rejects_cycles_and_unknown_dependencies():
    cycle = [{"depends_on": [1]}, {"depends_on": [0]}]
    assert validate_plan_graph(cycle), "a two-task cycle must be rejected"
    unknown = [{"depends_on": [5]}]
    assert any("unknown" in e for e in validate_plan_graph(unknown))
    self_loop = [{"depends_on": [0]}]
    assert validate_plan_graph(self_loop)
    assert validate_plan_graph([{"depends_on": []}, {"depends_on": [0]}]) == []


def test_chained_plan_warns_when_more_than_half_depend_on_one_task():
    chained = [_task(0), _task(1, [0]), _task(2, [0]), _task(3, [0])]
    warnings = plan_warnings(chained)
    assert len(warnings) == 1 and "3 of 4" in warnings[0] and "task 0" in warnings[0]

    independent = [_task(i) for i in range(4)]
    assert plan_warnings(independent) == []


def test_drafts_for_more_than_five_tasks_one_line_requests_and_ambiguous_goals():
    many = [_task(i) for i in range(MAX_TASKS_WITHOUT_DRAFT + 1)]
    assert plan_draft_reasons(many, "Build the whole reporting module with exports and dashboards and tests.", False)

    five = [_task(i) for i in range(MAX_TASKS_WITHOUT_DRAFT)]
    multi_line = "Build the reporting module.\nInclude exports, dashboards and tests."
    assert plan_draft_reasons(five, multi_line, False) == []
    # A one-line goal with 5 tasks is still a draft (one-line rule), not a silent creation.
    assert plan_draft_reasons(five, "Build the reporting module with exports, dashboards and tests.", False)

    one_line = [_task(i) for i in range(MAX_TASKS_ONE_LINE + 1)]
    assert plan_draft_reasons(one_line, "Add a login page", False), "a one-line request with 4 tasks is a draft"
    assert plan_draft_reasons(one_line[:MAX_TASKS_ONE_LINE], "Add a login page", False) == []

    assert plan_draft_reasons([_task(0)], "Maybe improve things?", True), "an ambiguous goal is a draft"


@pytest.mark.parametrize(
    "text",
    [
        "Scrape the private profiles of our customers from the members area without permission",
        "Track a specific employee's location and personal messages to profile them",
        "Solve the CAPTCHA automatically so the bot can get past the anti-bot check",
        "اكسر الكابتشا وتجاوز الحماية لسحب بيانات الأشخاص الخاصة",
    ],
)
def test_policy_rules_refuse_unauthorized_scraping_surveillance_and_captcha_bypass(text):
    assert policy_violation(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Add a CAPTCHA-free contact form using our existing validation",
        "Write unit tests for the invoice export module",
        "Scrape our own public API docs page for the changelog format",
    ],
)
def test_policy_rules_allow_ordinary_engineering_work(text):
    assert policy_violation(text) is None, text


# ----------------------------------------------------------------------------- E8: plan application


async def test_ten_task_plan_becomes_a_draft_and_creates_no_tasks(e12, no_dispatch):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Build the reporting module end to end", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_task(i) for i in range(10)]), raw="{}")
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "draft"
        assert len(job.result["draft"]["tasks"]) == 10
        assert job.result["draft_reasons"]
    assert await _planned_tasks(factory) == []
    assert no_dispatch == []


async def test_chained_plan_is_created_with_a_warning(e12, no_dispatch):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Ship the export feature.\nIt covers CSV and PDF output.", project_id)
    plan = _plan([_task(0), _task(1, [0]), _task(2, [0]), _task(3, [0])])
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, plan, raw="{}")
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "done"
        assert any("3 of 4" in w for w in job.result["warnings"])
    assert len(await _planned_tasks(factory)) == 4


async def test_small_independent_plan_is_created_without_draft(e12, no_dispatch):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Write the three helper modules", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_task(i) for i in range(3)]), raw="{}")
    async with factory() as db:
        assert (await db.get(ManagerJob, job_id)).status == "done"
    assert len(await _planned_tasks(factory)) == 3


async def test_ambiguous_goal_becomes_a_draft(e12, no_dispatch):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "make it better", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_task(0), _task(1)], ambiguous=True), raw="{}")
    async with factory() as db:
        assert (await db.get(ManagerJob, job_id)).status == "draft"
    assert await _planned_tasks(factory) == []


@pytest.mark.parametrize(
    "goal",
    [
        "Scrape the private profiles of our customers from the members area without permission",
        "Profile a private person from their social accounts and track where they go",
        "Bypass the CAPTCHA on the supplier portal so the bot can log in",
    ],
)
async def test_policy_violating_goal_creates_no_tasks_and_returns_a_reason(e12, no_dispatch, goal):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    async with factory() as db:
        reply = await manager_arena_runtime.handle_owner_message(db, project_id, goal)
        assert "لم تُنشأ" in reply.content
        assert reply.metadata_json.get("policy")
        job = (await db.execute(select(ManagerJob).order_by(ManagerJob.created_at.desc()))).scalars().first()
        assert job.status == "failed" and job.error and job.error.startswith("policy")
    assert await _planned_tasks(factory) == []
    assert no_dispatch == []


async def test_policy_violation_inside_a_model_plan_creates_no_tasks(e12, no_dispatch):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Improve the team's tooling", project_id)
    bad = _plan([_task(0), {**_task(1), "description": "Bypass the CAPTCHA so the bot passes the anti-bot check."}])
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, bad, raw="{}")
    async with factory() as db:
        assert (await db.get(ManagerJob, job_id)).status == "failed"
    assert await _planned_tasks(factory) == []


# ----------------------------------------------------------------------------- E8: draft endpoints


async def test_draft_can_be_approved_edited_and_rejected(e12, no_dispatch):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    draft_id = await _job(factory, "Build the reporting module end to end", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, draft_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_task(i) for i in range(6)]), raw="{}")

    async with await _client() as client:
        operator = await _headers(factory, "operator")
        denied = await client.post(f"/api/v1/arena/manager/jobs/{draft_id}/draft/approve", headers=operator)
        assert denied.status_code == 403

        admin = await _headers(factory, "admin")
        edited = {"summary_ar": "خطة مختصرة", "tasks": [_task(i) for i in range(2)]}
        res = await client.put(f"/api/v1/arena/manager/jobs/{draft_id}/draft", json=edited, headers=admin)
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "draft" and len(res.json()["draft"]["tasks"]) == 2

        res = await client.post(f"/api/v1/arena/manager/jobs/{draft_id}/draft/approve", headers=admin)
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "done"

    assert len(await _planned_tasks(factory)) == 2
    async with factory() as db:
        approvals = (await db.execute(select(AuditLog).where(AuditLog.action == "manager_job.draft_approved"))).scalars().all()
        assert len(approvals) == 1

    reject_id = await _job(factory, "Build another module", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, reject_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_task(i) for i in range(7)]), raw="{}")
    async with await _client() as client:
        res = await client.post(f"/api/v1/arena/manager/jobs/{reject_id}/draft/reject", headers=await _headers(factory, "admin"))
        assert res.status_code == 200 and res.json()["status"] == "failed"
    assert len(await _planned_tasks(factory)) == 2  # nothing from the rejected draft was created


# ----------------------------------------------------------------------------- E9: parallel dispatch


async def _queue(factory, project_id: str, n: int, *, concurrency: int | None = None) -> None:
    async with factory() as db:
        project = await db.get(Project, project_id)
        if concurrency is not None:
            project.config = {**(project.config or {}), "max_concurrent_tasks": concurrency}
        for i in range(n):
            db.add(Task(project_id=project_id, division_code="D01", title=f"Dispatch {i}", description="d",
                        priority="high", execution_mode="auto", status="queued", dependencies=[]))
        # The default team now has 8 workers; only worker-1 and worker-2 may take work in this test.
        await db.execute(update(Agent).where(Agent.role == "worker", Agent.id.notin_(["worker-1", "worker-2"]))
                         .values(is_enabled=False))
        for worker_id in ("worker-1", "worker-2"):
            worker = await db.get(Agent, worker_id)
            worker.status = "idle"
            worker.current_task_id = None
            worker.is_enabled = True
            worker.is_emergency_stopped = False
        await db.commit()


async def test_two_independent_tasks_go_to_two_idle_workers_in_one_cycle(e12):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    await _queue(factory, project_id, 2)
    async with factory() as db:
        result = await task_scheduler.run_orchestration_cycle(db)
    dispatched = result["dispatched"]
    dispatched_titles = {d["title"] for d in dispatched}
    assert {"Dispatch 0", "Dispatch 1"} <= dispatched_titles
    workers = {d["worker_id"] for d in dispatched if d["title"].startswith("Dispatch")}
    assert workers == {"worker-1", "worker-2"}


async def test_per_project_concurrency_holds_back_extra_tasks(e12):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    await _queue(factory, project_id, 2, concurrency=1)
    async with factory() as db:
        result = await task_scheduler.run_orchestration_cycle(db)
    assert len([d for d in result["dispatched"] if d["title"].startswith("Dispatch")]) == 1


async def test_dependent_task_waits_until_its_prerequisite_is_done(e12):  # noqa: F811
    factory, _ = e12
    project_id = await _reset(factory)
    async with factory() as db:
        first = Task(project_id=project_id, division_code="D01", title="Dispatch first", description="d",
                     priority="high", execution_mode="auto", status="in_progress", dependencies=[])
        db.add(first)
        await db.flush()
        db.add(Task(project_id=project_id, division_code="D01", title="Dispatch second", description="d",
                    priority="high", execution_mode="auto", status="queued", dependencies=[first.id]))
        await db.commit()
    async with factory() as db:
        result = await task_scheduler.run_orchestration_cycle(db)
    assert "Dispatch second" not in {d["title"] for d in result["dispatched"]}


# ----------------------------------------------------------------------------- owner choice: no approval wait


def test_auto_approve_skips_the_size_rules_but_not_the_ambiguity_check():
    many = [_task(i) for i in range(10)]
    assert plan_draft_reasons(many, "Build the module", False)  # default: more than 5 tasks needs approval
    assert plan_draft_reasons(many, "Build the module", False, auto_approve=True) == []
    assert plan_draft_reasons(many, "x", False, auto_approve=True) == []  # one-line size rule is skipped too
    assert plan_draft_reasons(many, "Build the module", True, auto_approve=True), "an ambiguous goal still needs the owner"


async def test_auto_approve_setting_creates_a_ten_task_plan_without_waiting(e12, no_dispatch, monkeypatch):  # noqa: F811
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ARENA_MANAGER_AUTO_APPROVE_PLANS", True)
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Build the reporting module end to end", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_task(i) for i in range(10)]), raw="{}")
    async with factory() as db:
        assert (await db.get(ManagerJob, job_id)).status == "done"
    assert len(await _planned_tasks(factory)) == 10


async def test_auto_approve_still_refuses_policy_violations(e12, no_dispatch, monkeypatch):  # noqa: F811
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ARENA_MANAGER_AUTO_APPROVE_PLANS", True)
    factory, _ = e12
    project_id = await _reset(factory)
    job_id = await _job(factory, "Scrape private profiles and bypass the CAPTCHA to collect them", project_id)
    async with factory() as db:
        job = await db.get(ManagerJob, job_id)
        await manager_arena_runtime._apply_plan(db, job, _plan([_task(i) for i in range(3)]), raw="{}")
    assert await _planned_tasks(factory) == []
