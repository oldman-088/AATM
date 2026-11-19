"""A project can send all work to its own base branch; nothing may reach the default branch by accident."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_d_arena_connector
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_d_arena_connector import _admin_headers, arena_settings, env  # noqa: F401 - fixtures
from test_r3_arena_agent_workflow import _runner_fixture

from app.db.base import utcnow
from app.git_workflows.manager import git_workflow_manager
from app.main import app
from app.models import Agent, ApprovalRequest, ArenaSession, Notification, Project, Task
from app.models.project import configured_base_branch, project_base_branch
from app.orchestration.arena_prompts import base_branch_publish_steps, build_task_prompt, manager_role_block
from app.orchestration.arena_runner import ArenaAgentRunner
from app.orchestration.review import review_service

REPO = "owner/demo"
REPO_URL = f"https://github.com/{REPO}"


def _project(**extra: Any) -> Project:
    return Project(name="Demo", slug="demo", repo_path=".", repo_url=REPO_URL, default_branch="main",
                   tech_stack=[], config={"base_branch": "r8-aatm"}, **extra)


def _gh_recorder(monkeypatch, base: str | None):
    calls: list[list[str]] = []

    async def _exec(cmd, cwd, timeout=30):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "view"]:
            return (0, json.dumps({"baseRefName": base}), "") if base else (1, "", "not found")
        return 0, "", ""

    monkeypatch.setattr(git_workflow_manager, "_exec", _exec)
    return calls


def test_base_branch_falls_back_to_the_default_branch():
    assert project_base_branch(None) == "main"
    plain = Project(name="p", slug="p", default_branch="develop", config={})
    assert project_base_branch(plain) == "develop" and configured_base_branch(plain) is None
    assert project_base_branch(_project()) == "r8-aatm" and configured_base_branch(_project()) == "r8-aatm"


def test_worker_prompt_preflights_the_expected_base_head_before_any_task_instructions():
    task = Task(id="task-1", title="Add x", description="Add x", priority="high", expected_deliverables=[], checkpoints=[])
    expected_head = "a" * 40
    prompt = build_task_prompt(task, _project(), expected_head_sha=expected_head)
    assert prompt.startswith("## Mandatory base-branch preflight")
    assert "git fetch origin r8-aatm" in prompt
    assert "git checkout -B r8-aatm origin/r8-aatm" in prompt
    assert "git pull --ff-only origin r8-aatm" in prompt
    assert "git rev-parse HEAD" in prompt
    assert "Base branch: r8-aatm" in prompt
    assert f"Expected head SHA from AATM: {expected_head}" in prompt
    assert "STOP" in prompt and "differs" in prompt


def test_worker_and_manager_prompts_name_only_the_project_base_branch():
    task = Task(id="task-1", title="Add x", description="Add x", priority="high", expected_deliverables=[], checkpoints=[])
    prompt = build_task_prompt(task, _project(), expected_head_sha="b" * 40)
    assert "Base branch: r8-aatm" in prompt
    assert "Start your branch from the latest `origin/r8-aatm`" in prompt
    assert "Open the pull request against `r8-aatm`. Never target `main`" in prompt
    assert "targets the base branch `main`" not in prompt
    assert "every pull request targets `r8-aatm`" in manager_role_block([], _project())


def test_project_without_a_base_branch_gets_no_extra_rules():
    task = Task(id="task-1", title="Add x", description="Add x", priority="high", expected_deliverables=[], checkpoints=[])
    plain = Project(name="p", slug="p", repo_path=".", repo_url=REPO_URL, default_branch="main", tech_stack=[], config={})
    assert "Start your branch from" not in build_task_prompt(task, plain, expected_head_sha="c" * 40)


async def test_merge_is_refused_when_the_pull_request_targets_another_branch(monkeypatch, tmp_path):
    calls = _gh_recorder(monkeypatch, "main")
    result = await git_workflow_manager.merge_pull_request(repo_slug=REPO, pr_number=7, cwd=tmp_path, expected_base="r8-aatm")
    assert result["ok"] is False and result["code"] == "wrong_base"
    assert not any(c[:3] in (["gh", "pr", "merge"], ["gh", "pr", "ready"]) for c in calls)


async def test_merge_is_refused_when_the_base_cannot_be_read(monkeypatch, tmp_path):
    calls = _gh_recorder(monkeypatch, None)
    result = await git_workflow_manager.merge_pull_request(repo_slug=REPO, pr_number=7, cwd=tmp_path, expected_base="r8-aatm")
    assert result["ok"] is False and result["code"] == "wrong_base"
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


async def test_merge_runs_when_the_pull_request_targets_the_base_branch(monkeypatch, tmp_path):
    calls = _gh_recorder(monkeypatch, "r8-aatm")
    result = await git_workflow_manager.merge_pull_request(repo_slug=REPO, pr_number=7, cwd=tmp_path, expected_base="r8-aatm")
    assert result["ok"] is True and any(c[:3] == ["gh", "pr", "merge"] for c in calls)


async def test_approved_merge_passes_the_base_branch_to_the_merge_guard(env, monkeypatch):
    seen: dict[str, Any] = {}

    async def _merge(**kwargs):
        seen.update(kwargs)
        return {"ok": False, "code": "wrong_base", "error": "refused"}

    monkeypatch.setattr(git_workflow_manager, "merge_pull_request", _merge)
    async with env() as db:
        project = _project()
        db.add(project)
        await db.flush()
        task = Task(project_id=project.id, title="Add x", description="", status="approved", pr_number=7)
        db.add(task)
        await db.flush()
        approval = ApprovalRequest(project_id=project.id, action_type="merge", title="Merge", description="", requested_by="manager-0",
                                   status="approved", payload={"task_id": task.id, "repo": REPO, "base_branch": "r8-aatm", "pr_number": 7})
        db.add(approval)
        await db.commit()
        result = await review_service.merge_task(db, task, approval, actor="owner")
    assert seen["expected_base"] == "r8-aatm"
    assert result["ok"] is False and task.status != "merged"


async def test_pull_request_on_the_wrong_base_is_escalated_and_not_tracked(env, arena_settings, monkeypatch):
    async def _find(**_kwargs):
        return {"ok": True, "pull_request": {"number": 9, "url": f"{REPO_URL}/pull/9", "state": "OPEN", "baseRefName": "main",
                                             "headRefName": "arena/x", "headRefOid": "abc"}}

    monkeypatch.setattr(git_workflow_manager, "find_task_pull_request", _find)
    async with env() as db:
        project = _project()
        db.add(project)
        await db.flush()
        agent = await db.get(Agent, "worker-1")
        task = Task(project_id=project.id, title="Add x", description="", status="in_progress", assigned_agent_id=agent.id)
        db.add(task)
        await db.flush()
        session = ArenaSession(agent_id=agent.id, profile_id=agent.browser_profile_id, task_id=task.id, active_task_id=task.id,
                               phase="working", page_state="generating", submitted_at=utcnow(), last_state_change_at=utcnow(), events=[])
        db.add(session)
        await db.commit()
        result = await ArenaAgentRunner(object())._poll_github(db, session, task, project)
        notes = list((await db.execute(select(Notification).where(Notification.severity == "critical"))).scalars())
    assert result["wrong_base"] is True and session.phase == "escalated" and session.pr_number is None and task.pr_number is None
    assert any("r8-aatm" in n.message for n in notes)


async def test_api_sets_base_branch_and_default_project_and_disables_projects(env):
    headers = await _admin_headers(env)
    async with env() as db:
        first, second = Project(name="first", slug="first", config={}), Project(name="second", slug="second", config={"active": True})
        target = Project(name="target", slug="target", config={})
        db.add_all([first, second, target])
        await db.commit()
        first_id, second_id, target_id = first.id, second.id, target.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bad = await client.patch(f"/api/v1/projects/{target_id}", json={"base_branch": "bad..name"}, headers=headers)
        assert bad.status_code == 422
        ok = await client.patch(f"/api/v1/projects/{target_id}", json={"base_branch": "r8-aatm", "active": True}, headers=headers)
        assert ok.status_code == 200, ok.text
        off = await client.patch(f"/api/v1/projects/{first_id}", json={"status": "disabled"}, headers=headers)
        assert off.status_code == 200 and off.json()["status"] == "disabled"
        refused = await client.patch(f"/api/v1/projects/{first_id}", json={"active": True}, headers=headers)
        assert refused.status_code == 409
        projects = (await client.get("/api/v1/projects", headers=headers)).json()
    assert projects[0]["id"] == target_id and projects[0]["config"]["base_branch"] == "r8-aatm"
    assert "active" not in next(p for p in projects if p["id"] == second_id)["config"]


def _git(cwd: Path, *args: str) -> str:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    return subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def test_publish_steps_put_work_from_a_default_branch_chat_on_top_of_the_moved_base(tmp_path):
    """Arena opens the chat branch from main; the base has moved on, which caused "No commits between"."""
    origin, seed, chat = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "chat"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    _git(seed, "checkout", "-q", "-b", "main")
    _commit(seed, "first")
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "checkout", "-q", "-b", "r8-aatm")
    base_head = _commit(seed, "base-work")
    _git(seed, "push", "-q", "origin", "r8-aatm")
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(chat)], check=True, capture_output=True)
    _git(chat, "config", "user.name", "Test")
    _git(chat, "config", "user.email", "test@example.invalid")
    _git(chat, "checkout", "-q", "-b", "arena/abc-demo")
    _git(chat, "push", "-q", "origin", "arena/abc-demo")
    _commit(chat, "worker-change")
    assert _git(origin, "rev-list", "--count", "r8-aatm..arena/abc-demo") == "0", "the failure: nothing on GitHub to open a PR from"

    steps = base_branch_publish_steps(_project())
    for step in (steps[2], steps[3]):
        subprocess.run(re.search(r"`([^`]+)`", step).group(1), cwd=chat, shell=True, check=True, capture_output=True)  # noqa: S602

    assert _git(origin, "rev-list", "--count", "r8-aatm..arena/abc-demo") == "1"
    assert _git(origin, "merge-base", "r8-aatm", "arena/abc-demo") == base_head
    assert _git(chat, "rev-parse", "HEAD") == _git(origin, "rev-parse", "arena/abc-demo")


async def test_no_pull_request_nudge_repeats_the_publish_steps(env, arena_settings, monkeypatch, tmp_path):
    page, runner, _gh, (task_id, _session, _agent), _ = await _runner_fixture(env, arena_settings, monkeypatch, tmp_path)
    async with env() as db:
        task = await db.get(Task, task_id)
        (await db.get(Project, task.project_id)).config = {"base_branch": "r8-aatm"}
        await db.commit()
        page.reply = "All done!"
        for _ in range(4):
            page.page_state = "idle"
            await runner.tick(db)
    nudge = next(text for command, text in page.commands if command == "submit")
    assert f"[AATM-{task_id}]" in nudge and "git rebase origin/r8-aatm" in nudge and "No commits between" in nudge
