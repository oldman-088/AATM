"""P1 PR card: CI and conflict state of a task's pull request."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_e_dashboard
from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from test_e_dashboard import _client_headers, env  # noqa: F401 - fixture

from app.github_client.client import github_client, summarize_checks
from app.main import app
from app.models import Project, Task


def test_ci_verdict_is_failure_then_pending_then_success():
    ok = {"__typename": "CheckRun", "name": "gates", "status": "COMPLETED", "conclusion": "SUCCESS"}
    skipped = {"__typename": "CheckRun", "name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"}
    running = {"__typename": "CheckRun", "name": "e2e", "status": "IN_PROGRESS", "conclusion": ""}
    failed = {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"}
    legacy_ok = {"__typename": "StatusContext", "context": "ci/legacy", "state": "SUCCESS"}
    assert summarize_checks([]) == ("none", [])
    assert summarize_checks([ok, skipped, legacy_ok])[0] == "success"
    assert summarize_checks([ok, running])[0] == "pending"
    verdict, checks = summarize_checks([ok, running, failed])
    assert verdict == "failure" and [c["name"] for c in checks if c["result"] == "failure"] == ["lint"]
    assert summarize_checks([{"__typename": "StatusContext", "context": "x", "state": "PENDING"}])[0] == "pending"


async def test_pr_state_route_reads_the_project_repository(env, monkeypatch):
    seen = {}

    async def fake_state(repo, number):
        seen["args"] = (repo, number)
        return {"available": True, "state": "OPEN", "conflicting": True, "ci": "failure", "checks": []}

    monkeypatch.setattr(github_client, "pull_request_state", fake_state)
    async with env() as db:
        project = Project(name="P", slug="p-prstate", repo_url="https://github.com/acme/demo", default_branch="main")
        db.add(project)
        await db.flush()
        task = Task(project_id=project.id, title="T", description="d", status="pr_open", pr_number=7)
        db.add(task)
        await db.commit()
        task_id = task.id
    headers = await _client_headers(env)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(f"/api/v1/reviews/tasks/{task_id}/pr-state", headers=headers)
        assert r.status_code == 200 and r.json()["conflicting"] is True and seen["args"] == ("acme/demo", 7)
        assert (await client.get("/api/v1/reviews/tasks/missing/pr-state", headers=headers)).status_code == 404


async def test_invalid_repo_is_refused_before_gh_runs():
    assert (await github_client.pull_request_state("not a repo", 1))["available"] is False
