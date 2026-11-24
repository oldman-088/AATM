"""P0 / E45: the high-risk gate judges intent, and approving it releases the waiting plan."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_r7_manager_arena
from __future__ import annotations

import pytest
from sqlalchemy import select
from test_r7_manager_arena import (  # noqa: F401 - fixtures
    _jobs,
    arena_settings,
    env,
    scripted,
)

from app.models import ApprovalRequest, Task
from app.orchestration.scheduler import high_risk_directive_categories

GOOD_PLAN = (
    '```json\n{"summary_ar": "خطة", "tasks": [{"title": "Add answer()", "assignee": "worker-1", '
    '"description": "answer() returns 42", "acceptance_criteria": ["answer() returns 42"], "depends_on": []}]}\n```'
)


def _kinds(text: str) -> set[str]:
    return {kind for kind, _ in high_risk_directive_categories(text)}


@pytest.mark.parametrize("text", [
    "Each worker opens a pull request against main.",
    "Open a PR into main when the tests pass.",
    "Before you start, rebase your branch onto the latest main.",
    "Bring your branch up to date with origin/main.",
    "Merge main into your feature branch if it falls behind.",
    "Pull the latest main and branch off main.",
    "Do not push to main.",
    "Never merge into main yourself.",
    "Workers must not push directly to main.",
])
def test_standard_pull_request_workflow_is_not_gated(text):
    assert "merge" not in _kinds(text)


@pytest.mark.parametrize("text", [
    "Merge the feature branch into main now.",
    "Push the fix to main.",
    "Don't wait for review, just push to main.",
    "Open a pull request against main, then merge it into main.",
    "Do not open a PR; push to main.",
    "Update the PR and push to main.",
    "Rebase, then force push onto main.",
    "force push to master",
])
def test_real_protected_branch_changes_are_still_gated(text):
    assert "merge" in _kinds(text)


def test_other_categories_still_gate():
    assert "delete" in _kinds("Drop the production database.")
    assert "deploy" in _kinds("Deploy to production tonight.")


async def _gated_plan(env, make, project_id, status: str = "pending"):
    page, runtime = make({"PLAN": [GOOD_PLAN]})
    async with env() as db:
        approval = ApprovalRequest(project_id=project_id, action_type="merge", title="Merge or push into a protected branch",
                                   description="d", requested_by="manager-0", status=status,
                                   payload={"directive": "merge it into main", "action_type": "merge",
                                            "chat_message_id": "owner-msg-1"})
        db.add(approval)
        await db.commit()
        approval_id = approval.id
        await runtime.handle_owner_message(db, project_id, "ابنِ دالة answer() then merge it into main",
                                           user_message_id="owner-msg-1", gated_categories={"merge"})
    await runtime.wait_idle()
    return runtime, approval_id


async def _plan_job(env):
    return next(j for j in await _jobs(env) if j.kind == "plan")


async def test_approving_the_gate_releases_the_deferred_plan(env, scripted):
    make, project_id = scripted
    runtime, approval_id = await _gated_plan(env, make, project_id)
    job = await _plan_job(env)
    assert job.status == "done" and job.result["deferred"] == ["merge"] and "task_ids" not in job.result

    async with env() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        approval.status = "approved"
        await db.commit()
        outcome = await runtime.release_deferred_plan(db, approval)
    assert outcome is not None and outcome["released"] is True

    job = await _plan_job(env)
    assert "deferred" not in job.result and job.result["released_by_approval"] == approval_id
    async with env() as db:
        tasks = (await db.execute(select(Task).where(Task.id.in_(job.result["task_ids"])))).scalars().all()
        assert [t.assigned_agent_id for t in tasks] == ["worker-1"]
        # Releasing twice never creates the tasks twice.
        assert await runtime.release_deferred_plan(db, approval) is None


async def test_rejecting_the_gate_cancels_the_deferred_plan(env, scripted):
    make, project_id = scripted
    runtime, approval_id = await _gated_plan(env, make, project_id)
    async with env() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        approval.status = "rejected"
        await db.commit()
        outcome = await runtime.release_deferred_plan(db, approval)
    assert outcome == {"job_id": (await _plan_job(env)).id, "released": False, "rejected": True}
    job = await _plan_job(env)
    assert job.status == "failed" and "task_ids" not in job.result


async def test_gate_approved_before_the_plan_arrives_creates_the_tasks_at_once(env, scripted):
    make, project_id = scripted
    await _gated_plan(env, make, project_id, status="approved")
    job = await _plan_job(env)
    assert job.status == "done" and "deferred" not in job.result and len(job.result["task_ids"]) == 1


async def test_unlinked_gate_keeps_the_plan_waiting(env, scripted):
    make, project_id = scripted
    page, runtime = make({"PLAN": [GOOD_PLAN]})
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, "ابنِ دالة answer() then merge it into main",
                                           gated_categories={"merge"})
    await runtime.wait_idle()
    job = await _plan_job(env)
    assert job.result["deferred"] == ["merge"] and "task_ids" not in job.result
