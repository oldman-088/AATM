"""P2 merge queue: merge only on approve + green CI + no conflict, one at a time; pause on a red base."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_e_dashboard
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from test_e_dashboard import _client_headers, env  # noqa: F401 - fixture

from app.git_workflows.manager import git_workflow_manager
from app.models import ApprovalRequest, Notification, Project, Task
from app.orchestration.merge_queue import ACTOR, MergeQueue


class FakeGitHub:
    def __init__(self) -> None:
        self.pr: dict[str, Any] = {"available": True, "state": "OPEN", "base": "main", "head_sha": "a" * 40,
                                   "conflicting": False, "ci": "success", "checks": []}
        self.base: dict[str, Any] = {"available": True, "head_sha": "b" * 40, "ci": "success", "checks": []}

    async def pull_request_state(self, repo: str, number: int) -> dict[str, Any]:
        return dict(self.pr, number=number)

    async def branch_ci(self, repo: str, branch: str) -> dict[str, Any]:
        return dict(self.base)


async def _setup(env, *, auto_merge: bool = True, escalated: bool = False) -> tuple[str, str, str]:
    async with env() as db:
        project = Project(name=f"Q-{auto_merge}-{escalated}", slug=f"q-{auto_merge}-{escalated}", repo_url="https://github.com/acme/demo",
                          default_branch="main", config={"auto_merge": auto_merge})
        db.add(project)
        await db.flush()
        task = Task(project_id=project.id, title="Add answer()", description="d", status="in_review", review_status="reviewed",
                    task_type="implementation", pr_number=7, assigned_agent_id="worker-1", branch_name="aatm/t1")
        db.add(task)
        await db.flush()
        approval = ApprovalRequest(project_id=project.id, action_type="merge", title="Merge PR #7", description="d",
                                   requested_by="manager-0", status="pending",
                                   payload={"task_id": task.id, "pr_number": 7, "repo": "acme/demo", "target_branch": "main",
                                            "escalated": escalated})
        db.add(approval)
        await db.commit()
        return project.id, task.id, approval.id


def _merges(monkeypatch) -> list[int]:
    merged: list[int] = []

    async def fake_merge(*, repo_slug, pr_number, cwd, expected_base=None):
        merged.append(pr_number)
        return {"ok": True}

    monkeypatch.setattr(git_workflow_manager, "merge_pull_request", fake_merge)
    return merged


async def _tick(env, queue: MergeQueue, project_id: str) -> dict:
    async with env() as db:
        return await queue.project_tick(db, await db.get(Project, project_id))


async def test_approved_green_pr_is_merged_and_the_base_is_checked_next(env, monkeypatch):
    merged = _merges(monkeypatch)
    gh = FakeGitHub()
    queue = MergeQueue(gh, interval=0)
    project_id, task_id, approval_id = await _setup(env)
    assert (await _tick(env, queue, project_id))["status"] == "merged" and merged == [7]
    async with env() as db:
        approval, task, project = await db.get(ApprovalRequest, approval_id), await db.get(Task, task_id), await db.get(Project, project_id)
        assert approval.status == "approved" and approval.reviewed_by == ACTOR
        assert task.status in {"merged", "done"}
        assert project.config["merge_queue"]["awaiting_base"] is True
    gh.base["ci"] = "pending"
    assert (await _tick(env, queue, project_id))["status"] == "waiting_base"
    gh.base["ci"] = "success"
    assert (await _tick(env, queue, project_id))["status"] == "idle"


async def test_a_red_base_pauses_the_queue_with_one_repair_task_and_green_resumes_it(env, monkeypatch):
    _merges(monkeypatch)
    gh = FakeGitHub()
    queue = MergeQueue(gh, interval=0)
    project_id, _task_id, _ = await _setup(env)
    await _tick(env, queue, project_id)
    gh.base.update(ci="failure", checks=[{"name": "gates", "result": "failure"}])
    assert (await _tick(env, queue, project_id))["status"] == "paused"
    assert (await _tick(env, queue, project_id))["status"] == "paused"
    async with env() as db:
        repairs = [t for t in (await db.execute(select(Task).where(Task.project_id == project_id))).scalars() if t.title.startswith("Repair main")]
        assert len(repairs) == 1 and "gates" in repairs[0].description and repairs[0].priority == "high"
        assert (await db.get(Project, project_id)).config["merge_queue"]["paused"]
    gh.base["ci"] = "success"
    assert (await _tick(env, queue, project_id))["status"] == "idle"
    async with env() as db:
        assert not (await db.get(Project, project_id)).config["merge_queue"]["paused"]


async def test_conflict_sends_one_follow_up_to_the_same_worker(env, monkeypatch):
    merged = _merges(monkeypatch)
    gh = FakeGitHub()
    gh.pr["conflicting"] = True
    queue = MergeQueue(gh, interval=0)
    project_id, task_id, _ = await _setup(env)
    assert (await _tick(env, queue, project_id))["follow_up_created"] is True
    assert (await _tick(env, queue, project_id))["follow_up_created"] is False
    assert merged == []
    async with env() as db:
        follow = (await db.execute(select(Task).where(Task.parent_task_id == task_id))).scalars().all()
        assert len(follow) == 1 and follow[0].assigned_agent_id == "worker-1" and follow[0].task_type == "follow_up"
        assert "conflicts with main" in follow[0].description


async def test_red_pending_or_missing_ci_never_merges(env, monkeypatch):
    merged = _merges(monkeypatch)
    gh = FakeGitHub()
    queue = MergeQueue(gh, interval=0)
    project_id, _task_id, _ = await _setup(env)
    gh.pr["ci"] = "pending"
    assert (await _tick(env, queue, project_id))["status"] == "waiting_ci"
    gh.pr.update(ci="failure", checks=[{"name": "lint", "result": "failure"}])
    assert (await _tick(env, queue, project_id))["status"] == "ci_failed"
    assert (await _tick(env, queue, project_id))["status"] == "ci_failed"
    gh.pr["ci"] = "none"
    assert (await _tick(env, queue, project_id))["status"] == "no_ci"
    assert merged == []
    async with env() as db:
        titles = [n.title for n in (await db.execute(select(Notification))).scalars()]
        assert titles.count("CI failed on PR #7") == 1


async def test_without_auto_merge_or_with_an_escalated_review_nothing_merges(env, monkeypatch):
    merged = _merges(monkeypatch)
    queue = MergeQueue(FakeGitHub(), interval=0)
    off_id, _, _ = await _setup(env, auto_merge=False)
    escalated_id, _, _ = await _setup(env, escalated=True)
    async with env() as db:
        results = await queue.tick(db)
    assert off_id not in results and results[escalated_id]["status"] == "idle" and merged == []


async def test_owner_turns_auto_merge_on_and_it_is_audited(env):
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.models import AuditLog

    project_id, _, _ = await _setup(env, auto_merge=False)
    headers = await _client_headers(env)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.patch(f"/api/v1/projects/{project_id}", json={"auto_merge": True}, headers=headers)
    assert r.status_code == 200, r.text
    async with env() as db:
        assert (await db.get(Project, project_id)).config["auto_merge"] is True
        logs = (await db.execute(select(AuditLog).where(AuditLog.action == "project.auto_merge"))).scalars().all()
        assert len(logs) == 1 and logs[0].details == {"auto_merge": True}
