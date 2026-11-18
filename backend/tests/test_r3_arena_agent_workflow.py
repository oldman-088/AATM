"""R3 — Arena Agent as an employee execution engine, end to end.

The full workflow runs for real: the backend (scheduler, AgentLoop, arena runner, reviewer,
Approval Center API), the local bridge, the generated userscript in headless Chromium on the mock
Arena Agent page, and git (a bare ``origin`` that plays GitHub's storage). Only the ``gh`` CLI is
replaced: a fake answers the GitHub API calls (PR search, matching-refs, compare) from that real
bare repository, and records reviews/merges. The reviewer's model is a deterministic script that
bases its verdict on the evidence AATM gives it (report vs real diff).
"""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_d_arena_connector
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.browser.arena_connector import agent_new_session_url, arena_gate
from app.db.base import utcnow
from app.git_workflows.manager import git_workflow_manager
from app.main import app
from app.models import Agent, ApprovalRequest, ArenaSession, AuditLog, Notification, Project, Task
from app.orchestration.arena_prompts import (
    GITHUB_BLOCKED_MARKER,
    build_correction_prompt,
    build_task_prompt,
    compare_report_to_changes,
    report_is_complete,
    reports_github_blocked,
)
from app.orchestration.arena_runner import ArenaAgentRunner, arena_reservations, arena_runner
from app.orchestration.scheduler import TaskScheduler
from app.providers.adapters.arena_agent import ArenaAgentAdapter, keep_working_allowed
from app.providers.base import ProviderRequest
from app.runtime.agent_loop import AgentLoop
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_b_review_merge_completion import REPO_SLUG, _FnRouter, _git, _init_repo
from test_d_arena_connector import (  # noqa: F401 - fixtures
    _admin_headers,
    arena_settings,
    bridge,
    env,
    needs_chromium,
)

WORKFLOW_BLOCK = (
    "Work on ONE branch.\n\nCreate ONE pull request.\n\nPush changes after every completed implementation item.\n\n"
    "Do NOT merge the pull request yourself.\n\nPut [AATM-{id}] at the beginning of the pull request title."
)


def _report(files: list[str], tests: str) -> str:
    return "\n".join([
        "## AATM REPORT",
        "### Implementation summary", "Added answer() returning 42. PR: https://github.com/acme/demo/pull/101",
        "### Modified files", *[f"- {f}" for f in files],
        "### Completed requirements", "- answer() exists",
        "### Tests performed", "- pytest -q",
        "### Test results", tests,
        "### Known limitations", "- none",
        "### Outstanding work", "- none",
    ])


class FakeGitHub:
    """``gh`` replacement: GitHub API answers computed from the real bare origin repository."""

    def __init__(self, tmp_path: Path, origin: Path) -> None:
        self.origin = origin
        self.mirror = tmp_path / "gh-mirror"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.mirror)], check=True, capture_output=True)
        self.prs: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []

    def _fetch(self) -> None:
        _git(self.mirror, "fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*")

    def _branches(self) -> list[dict[str, Any]]:
        out = subprocess.run(["git", "ls-remote", str(self.origin), "refs/heads/arena/*"], capture_output=True, text=True).stdout
        return [{"ref": ref, "object": {"sha": sha}} for sha, ref in (line.split("\t") for line in out.splitlines() if line)]

    def _compare(self, base: str, head: str) -> dict[str, Any]:
        self._fetch()
        rng = f"origin/{base}...origin/{head}"
        files = []
        for line in _git(self.mirror, "diff", "--numstat", rng).splitlines():
            add, dele, name = line.split("\t")
            files.append({"filename": name, "status": "modified", "additions": int(add), "deletions": int(dele),
                          "changes": int(add) + int(dele), "patch": _git(self.mirror, "diff", rng, "--", name)})
        commits = [{"sha": sha, "commit": {"message": _git(self.mirror, "log", "-1", "--format=%s", sha).strip()}}
                   for sha in _git(self.mirror, "rev-list", "--reverse", f"origin/{base}..origin/{head}").split()]
        return {"ahead_by": len(commits), "behind_by": 0, "commits": commits, "files": files,
                "html_url": f"https://github.com/{REPO_SLUG}/compare/{base}...{head}"}

    async def exec(self, cmd: list[str], cwd: Path, timeout: int = 30):
        self.calls.append(list(cmd))
        args = cmd[1:]
        if args[:2] == ["pr", "list"]:
            search = args[args.index("--search") + 1]
            prefix = search.split('"')[1]
            return 0, json.dumps([pr for pr in self.prs.values() if pr["title"].startswith(prefix)]), ""
        if args[:1] == ["api"] and "/git/matching-refs/heads/arena/" in args[1]:
            return 0, json.dumps(self._branches()), ""
        if args[:1] == ["api"] and "/compare/" in args[1]:
            base, head = args[1].split("/compare/", 1)[1].split("...", 1)
            return 0, json.dumps(self._compare(base, head)), ""
        if args[:2] == ["pr", "merge"]:
            number = int(args[2])
            for pr in self.prs.values():
                if pr["number"] == number:
                    pr["state"] = "MERGED"
            return 0, "merged", ""
        return 0, "ok", ""

    def open_pr(self, task_id: str, branch: str, sha: str) -> None:
        number = 101 + len(self.prs)
        self.prs[task_id] = {"number": number, "title": f"[AATM-{task_id}] Add answer()", "headRefName": branch,
                             "headRefOid": sha, "baseRefName": "main", "state": "OPEN", "isDraft": False,
                             "url": f"https://github.com/{REPO_SLUG}/pull/{number}", "createdAt": "", "author": {}}


class ArenaOnGitHub:
    """What the real Arena Agent does on GitHub: commits on its own arena/* branch and pushes."""

    def __init__(self, tmp_path: Path, origin: Path, gh: FakeGitHub) -> None:
        self.work = tmp_path / "arena-agent-clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.work)], check=True, capture_output=True)
        _git(self.work, "config", "user.name", "Arena Agent")
        _git(self.work, "config", "user.email", "agent@arena.invalid")
        self.gh = gh

    def push(self, task_id: str, branch: str, files: dict[str, str], message: str) -> str:
        _git(self.work, "fetch", "-q", "origin")
        existing = subprocess.run(["git", "rev-parse", "--verify", "-q", f"origin/{branch}"], cwd=self.work,
                                  capture_output=True).returncode == 0
        _git(self.work, "checkout", "-q", "-B", branch, f"origin/{branch}" if existing else "origin/main")
        for name, content in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-q", "-m", message)
        _git(self.work, "push", "-q", "origin", branch)
        sha = _git(self.work, "rev-parse", "HEAD").strip()
        if task_id not in self.gh.prs:
            self.gh.open_pr(task_id, branch, sha)
        self.gh.prs[task_id]["headRefOid"] = sha
        return sha


def _reviewer_model(request: ProviderRequest) -> str:
    data = json.loads(request.prompt)
    review = data.get("pull_request_review") or {}
    arena = review.get("arena_agent")
    assert arena is not None, "the reviewer must receive the Arena report and the real GitHub diff"
    assert "[UNTRUSTED" in arena["agent_report"] or "AATM REPORT" in arena["agent_report"]
    if arena["mechanical_discrepancies"]:
        return json.dumps({"action": "review", "event": "REQUEST_CHANGES",
                           "body": "Report does not match the diff: " + " ".join(arena["mechanical_discrepancies"])})
    assert {f["filename"] for f in arena["changed_files"]} == {"answer.py", "tests/test_answer.py"}
    return json.dumps({"action": "review", "event": "COMMENT", "body": "Report verified against the diff and tests."})


# --------------------------------------------------------------------------- prompt / report unit tests
def test_task_prompt_is_complete_and_contains_the_verbatim_github_workflow(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "ARCHITECTURE.md").write_text("FastAPI backend + React frontend.", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Use CSS logical properties.", encoding="utf-8")
    project = Project(name="P", slug="p", repo_path=str(repo), repo_url=f"https://github.com/{REPO_SLUG}.git", default_branch="main",
                      config={"test_command": "pytest -q", "acceptance_criteria": ["answer() returns 42"]})
    task = Task(id="task-123", title="Add answer()", description="Create answer() returning 42.", priority="high",
                expected_deliverables=["answer.py", "tests/test_answer.py"])
    prompt = build_task_prompt(task, project)
    for needle in ("Repository: acme/demo", "Base branch: main", "Create answer() returning 42.", "## Expected implementation",
                   "- answer.py", "## Acceptance criteria", "answer() returns 42", "FastAPI backend + React frontend.",
                   "Use CSS logical properties.", "## Testing requirements", "pytest -q", "## Security requirements",
                   "fake, placeholder or non-functional", "Preserve all existing functionality", "## AATM REPORT",
                   WORKFLOW_BLOCK.format(id="task-123")):
        assert needle in prompt, needle
    correction = build_correction_prompt(task, review_comments="Add the missing test.", discrepancies=["x.py not changed"],
                                         round_number=1, pr_url="https://github.com/acme/demo/pull/7")
    assert "SAME branch and the SAME pull request" in correction and "x.py not changed" in correction
    assert "Do not open a new pull request and do not merge" in correction


def test_report_comparison_finds_claims_that_do_not_match_the_diff():
    good = _report(["answer.py", "tests/test_answer.py"], "1 passed")
    assert report_is_complete(good)
    assert compare_report_to_changes(good, ["answer.py", "tests/test_answer.py"], commits=2) == []
    issues = compare_report_to_changes(good, ["answer.py"], commits=1)
    assert any("tests/test_answer.py" in i and "not changed" in i for i in issues)
    assert any("claims passing tests" in i for i in issues)
    issues = compare_report_to_changes(_report(["answer.py"], "1 passed"), ["answer.py", "secret_extra.py", "tests/test_answer.py"])
    assert any("missing from the report" in i and "secret_extra.py" in i for i in issues)
    assert compare_report_to_changes("", ["answer.py"]) == ["The agent did not provide a final report."]
    assert any("missing sections" in i for i in compare_report_to_changes("I did it.", ["answer.py"]))
    assert any("no commits" in i for i in compare_report_to_changes(good, ["answer.py", "tests/test_answer.py"], commits=0))


def test_new_session_on_real_arena_is_a_full_page_load(arena_settings, monkeypatch):
    monkeypatch.setattr(arena_settings, "ARENA_ALLOWED_ORIGINS", "https://arena.ai")
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_NEW_SESSION_URL", "")
    assert agent_new_session_url(True) == "https://arena.ai/agent"
    assert agent_new_session_url(False) is None  # the mock page keeps its New Chat button
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_NEW_SESSION_URL", "https://arena.ai/agent?new=1")
    assert agent_new_session_url(False) == "https://arena.ai/agent?new=1"


def test_keep_working_limit_zero_means_unlimited(arena_settings, monkeypatch):
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 0)
    assert all(keep_working_allowed(n) for n in (0, 10, 10_000))
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 3)
    assert keep_working_allowed(2) and not keep_working_allowed(3)


# --------------------------------------------------------------------------- runner with a scripted page
class ScriptedPage(ArenaAgentAdapter):
    """An Arena page whose state the test controls (no browser): records every command."""

    def __init__(self) -> None:
        super().__init__()
        self.page_state = "generating"
        self.commands: list[tuple[str, str | None]] = []
        self.reply = ""

    async def state(self, profile_id: str) -> dict[str, Any]:
        return {"ok": True, "connected": True, "mode": "agent", "status": "ready", "agent_state": self.page_state}

    async def command(self, profile_id: str, command: str, text: str | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        self.commands.append((command, text))
        if command == "keep_working":
            self.page_state = "generating"
            return {"clicked": True}
        if command == "read_last_reply":
            return {"text": self.reply, "truncated": False}
        if command == "submit":
            self.page_state = "generating"
            return {"sent": True}
        return {"ok": True}


async def _runner_fixture(env, arena_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", "http://127.0.0.1:9/mock-arena/")
    repo, origin = tmp_path / "demo", tmp_path / "origin.git"
    _init_repo(repo, origin)
    gh = FakeGitHub(tmp_path, origin)
    monkeypatch.setattr(git_workflow_manager, "_exec", _passthrough(gh))
    page = ScriptedPage()
    runner = ArenaAgentRunner(page)
    runner.github_poll_seconds = 0
    async with env() as db:
        project = Project(name="R3", slug="r3", repo_path=str(repo), repo_url=f"https://github.com/{REPO_SLUG}", default_branch="main")
        db.add(project)
        await db.flush()
        agent = await db.get(Agent, "worker-1")
        task = Task(project_id=project.id, title="Add answer()", description="answer() returns 42", status="in_progress",
                    assigned_agent_id=agent.id, execution_mode="manual")
        db.add(task)
        await db.flush()
        session = ArenaSession(agent_id=agent.id, profile_id=agent.browser_profile_id, task_id=task.id, active_task_id=task.id,
                               phase="working", page_state="generating", submitted_at=utcnow(), last_state_change_at=utcnow(),
                               events=[{"ts": utcnow().isoformat(), "kind": "submitted"}])
        db.add(session)
        agent.status, agent.current_task_id = "working", task.id
        await db.commit()
        ids = (task.id, session.id, agent.id)
    return page, runner, gh, ids, origin


def _passthrough(gh: FakeGitHub):
    original = git_workflow_manager._exec  # bound before monkeypatching

    async def _exec(cmd, cwd, timeout=30):
        if cmd and cmd[0] == "gh":
            return await gh.exec(cmd, cwd, timeout)
        return await original(cmd, cwd, timeout=timeout)

    return _exec


async def test_stall_without_meaningful_change_marks_needs_human_and_notifies(env, arena_settings, monkeypatch, tmp_path):
    page, runner, _gh, (task_id, session_id, agent_id), _ = await _runner_fixture(env, arena_settings, monkeypatch, tmp_path)
    async with env() as db:
        assert (await runner.tick(db))[session_id]["page"] == "generating"
        session = await db.get(ArenaSession, session_id)
        session.last_state_change_at = utcnow() - timedelta(minutes=21)
        session.submitted_at = session.last_state_change_at
        await db.commit()
        await runner.tick(db)
    async with env() as db:
        session, task, agent = await db.get(ArenaSession, session_id), await db.get(Task, task_id), await db.get(Agent, agent_id)
        assert session.phase == "needs_human" and "stalled" in session.needs_human_reason
        assert task.status == "blocked" and any(c.get("event") == "needs_human" for c in task.checkpoints)
        assert agent.status == "needs_human"
        notes = list((await db.execute(select(Notification).where(Notification.category == "arena"))).scalars())
        assert any("needs you" in n.title for n in notes)
        # The blocked task keeps the employee reserved for this session until a human resumes it.
        assert (await arena_reservations(db))[agent_id] == task_id


async def test_agent_without_github_access_goes_to_a_human_at_once(env, arena_settings, monkeypatch, tmp_path):
    page, runner, _gh, (task_id, session_id, agent_id), _ = await _runner_fixture(env, arena_settings, monkeypatch, tmp_path)
    prompt = build_task_prompt(Task(id="task-1", title="T", description="D"), None)
    assert GITHUB_BLOCKED_MARKER in prompt and not reports_github_blocked(prompt)  # an echoed prompt is not a report
    page.page_state = "keep_working_visible"
    page.reply = f"I checked.\n**{GITHUB_BLOCKED_MARKER}** no connector for this account"
    async with env() as db:
        result = (await runner.tick(db))[session_id]
    assert result["github_blocked"] is True
    assert ("keep_working", None) not in page.commands
    async with env() as db:
        session, task, agent = await db.get(ArenaSession, session_id), await db.get(Task, task_id), await db.get(Agent, agent_id)
        assert session.phase == "needs_human" and "no GitHub access" in session.needs_human_reason
        assert task.status == "blocked" and agent.status == "needs_human"


async def test_keep_working_continues_when_github_is_not_reported_blocked(env, arena_settings, monkeypatch, tmp_path):
    page, runner, _gh, (_task, session_id, _agent), _ = await _runner_fixture(env, arena_settings, monkeypatch, tmp_path)
    page.page_state = "keep_working_visible"
    page.reply = "Still implementing."
    async with env() as db:
        assert (await runner.tick(db))[session_id]["keep_working"] == 1


async def test_unlimited_keep_working_then_no_pr_is_escalated_instead_of_completed(env, arena_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 0)
    page, runner, _gh, (task_id, session_id, _agent), _ = await _runner_fixture(env, arena_settings, monkeypatch, tmp_path)
    async with env() as db:
        for _ in range(7):  # Keep working shown again and again: AATM continues every time
            page.page_state = "keep_working_visible"
            await runner.tick(db)
        assert sum(1 for c, _ in page.commands if c == "keep_working") == 7
        page.page_state = "idle"
        page.reply = "All done!"
        for _ in range(12):
            await runner.tick(db)
            page.page_state = "idle"
    nudges = [t for c, t in page.commands if c == "submit"]
    assert len(nudges) == 2 and all(f"[AATM-{task_id}]" in t and "Do NOT merge" in t for t in nudges)
    async with env() as db:
        session, task = await db.get(ArenaSession, session_id), await db.get(Task, task_id)
        assert session.keep_working_count == 7
        assert session.phase == "escalated" and task.status == "blocked"
        assert task.status not in {"completed", "pr_open", "done"}
        assert "without a pull request" in task.error_message
        assert (await db.execute(select(AuditLog).where(AuditLog.action == "arena.task_escalated"))).scalars().first()


async def test_keep_working_limit_stops_continuing_and_notifies(env, arena_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 2)
    page, runner, _gh, (_task, session_id, _agent), _ = await _runner_fixture(env, arena_settings, monkeypatch, tmp_path)
    async with env() as db:
        for _ in range(4):
            page.page_state = "keep_working_visible"
            await runner.tick(db)
        assert sum(1 for c, _ in page.commands if c == "keep_working") == 2
        session = await db.get(ArenaSession, session_id)
        assert any(e["kind"] == "keep_working_limit" for e in session.events)


async def test_pr_merged_outside_aatm_is_flagged(env, arena_settings, monkeypatch, tmp_path):
    page, runner, gh, (task_id, session_id, _agent), origin = await _runner_fixture(env, arena_settings, monkeypatch, tmp_path)
    ArenaOnGitHub(tmp_path, origin, gh).push(task_id, "arena/x1", {"answer.py": "def answer():\n    return 42\n"}, "answer")
    gh.prs[task_id]["state"] = "MERGED"
    async with env() as db:
        await runner.tick(db)
        session = await db.get(ArenaSession, session_id)
        assert session.phase == "escalated"
        assert any(e["kind"] == "merged_outside_approval" for e in session.events)
        assert (await db.execute(select(AuditLog).where(AuditLog.action == "arena.pr_merged_without_approval"))).scalars().first()


# --------------------------------------------------------------------------- full workflow in Chromium
async def _drive(env, scheduler, loops, until, *, timeout: float = 90.0, on_tick=None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with env() as db:
            await scheduler.run_orchestration_cycle(db)
        await asyncio.gather(*(loop.process_one() for loop in loops))
        if on_tick is not None:
            await on_tick()
        async with env() as db:
            if await until(db):
                return
        await asyncio.sleep(0.25)
    raise AssertionError("workflow condition not reached in time")


@needs_chromium
async def test_full_arena_agent_workflow_with_review_corrections_approval_and_new_session(
    env, bridge, arena_settings, monkeypatch, tmp_path
):
    from app.browser.manager import detect_playwright_capabilities
    from playwright.async_api import async_playwright

    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", f"{bridge.base}/mock-arena/")
    monkeypatch.setattr(arena_settings, "ARENA_BRIDGE_URL", bridge.base)
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_NEW_SESSION_URL", "")
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 0)
    monkeypatch.setattr(arena_runner, "github_poll_seconds", 0)
    assert arena_gate()["enabled"]

    repo, origin = tmp_path / "demo", tmp_path / "origin.git"
    _init_repo(repo, origin)
    gh = FakeGitHub(tmp_path, origin)
    monkeypatch.setattr(git_workflow_manager, "_exec", _passthrough(gh))
    arena = ArenaOnGitHub(tmp_path, origin, gh)
    headers = await _admin_headers(env)

    async with env() as db:
        project = Project(name="R3", slug="r3", repo_path=str(repo), repo_url=f"https://github.com/{REPO_SLUG}", default_branch="main",
                          config={"test_command": "pytest -q"})
        db.add(project)
        await db.flush()
        t1 = Task(project_id=project.id, title="Add answer()", description="Create answer() returning 42 with a test.",
                  status="queued", execution_mode="manual", assigned_agent_id="worker-1", division_code="D02",
                  expected_deliverables=["answer.py", "tests/test_answer.py"])
        db.add(t1)
        await db.commit()
        t1_id, project_id = t1.id, project.id
        profile_id = (await db.get(Agent, "worker-1")).browser_profile_id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=120) as client:
        r = await client.put("/api/v1/arena/agents/worker-1/mode", json={"mode": "agent"}, headers=headers)
        assert r.status_code == 200, r.text
        r = await client.post(f"/api/v1/arena/profiles/{profile_id}/userscript", headers=headers)
        assert r.status_code == 200 and r.json()["bridge_registration"]["ok"], r.text
        script = r.json()["script"]

        scheduler = TaskScheduler()
        loops = [AgentLoop(session_factory=env, provider_router=_FnRouter(_reviewer_model), poll_seconds=0.01) for _ in range(2)]
        executable = detect_playwright_capabilities().get("executable_path")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
            try:
                context = await browser.new_context()
                await context.add_init_script(script)
                page = await context.new_page()
                await page.goto(f"{bridge.base}/mock-arena/agent/?gen_ms=900&kw_rounds=2")
                # Round 1: the report claims a test file the agent never pushed.
                await page.evaluate("t => window.__mockArena.setReport(t)", _report(["answer.py", "tests/test_answer.py"], "1 passed"))
                prompts: list[str] = []

                async def _agent_on_github() -> None:
                    texts = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('[data-message-author-role=user]')).map(e => e.textContent)")
                    for t in texts:
                        if t not in prompts:
                            prompts.append(t)
                    if prompts and t1_id not in gh.prs:
                        arena.push(t1_id, "arena/a1b2c3", {"answer.py": "def answer():\n    return 42\n"}, "Add answer()")

                # 1. dispatch -> new_session -> submit -> Keep working x2 -> idle -> PR via GitHub API -> report -> review
                async def _review_requested_changes(db) -> bool:
                    return (await db.execute(select(Task).where(Task.parent_task_id == t1_id, Task.task_type == "follow_up"))).scalars().first() is not None

                await _drive(env, scheduler, loops, _review_requested_changes, on_tick=_agent_on_github)
                stats = await page.evaluate("() => window.__mockArena")
                assert stats["new_sessions"] == 1 and stats["submits"] == 1 and stats["kw_clicks"] == 2
                first = prompts[0]
                assert f"[AATM-{t1_id}]" in first and WORKFLOW_BLOCK.format(id=t1_id) in first
                assert "Repository: acme/demo" in first and "Base branch: main" in first and "pytest -q" in first
                async with env() as db:
                    parent = await db.get(Task, t1_id)
                    session = (await db.execute(select(ArenaSession).where(ArenaSession.task_id == t1_id))).scalar_one()
                    assert parent.pr_number == 101 and parent.branch_name == "arena/a1b2c3" and parent.status == "changes_requested"
                    assert session.pr_url == f"https://github.com/{REPO_SLUG}/pull/101" and session.keep_working_count == 2
                    assert "## AATM REPORT" in session.report
                    assert Path(parent.worktree_path).joinpath("answer.py").is_file()  # independent review checkout
                    review_posted = [c for c in gh.calls if c[1:3] == ["pr", "review"]]
                    assert review_posted and "--request-changes" in review_posted[0]

                # 2. correction goes to the SAME session; the agent pushes the missing test and reports again
                await page.evaluate("t => window.__mockArena.setReport(t)", _report(["answer.py", "tests/test_answer.py"], "1 passed"))

                async def _fix_on_github() -> None:
                    await _agent_on_github()
                    if len(prompts) >= 2 and len(_git(arena.work, "log", "--oneline", "origin/main..arena/a1b2c3").splitlines()) < 2:
                        arena.push(t1_id, "arena/a1b2c3", {"tests/test_answer.py": "from answer import answer\n\n\ndef test_answer():\n"
                                                           "    assert answer() == 42\n"}, "Add test")

                async def _merge_requested(db) -> bool:
                    approvals = (await db.execute(select(ApprovalRequest).where(ApprovalRequest.action_type == "merge"))).scalars().all()
                    return any((a.payload or {}).get("task_id") == t1_id for a in approvals)

                await _drive(env, scheduler, loops, _merge_requested, on_tick=_fix_on_github)
                stats = await page.evaluate("() => window.__mockArena")
                assert stats["new_sessions"] == 1, "corrections must reuse the same Arena session"
                assert stats["submits"] == 2 and stats["kw_clicks"] == 4
                correction = prompts[1]
                assert f"Corrections for [AATM-{t1_id}]" in correction and "tests/test_answer.py" in correction
                assert not any(c[1:3] == ["pr", "merge"] for c in gh.calls), "nothing merges before the owner approves"
                async with env() as db:
                    session = (await db.execute(select(ArenaSession).where(ArenaSession.task_id == t1_id))).scalar_one()
                    assert session.correction_rounds == 1 and session.is_open
                    approval = next(a for a in (await db.execute(select(ApprovalRequest))).scalars() if (a.payload or {}).get("task_id") == t1_id)
                    assert approval.status == "pending" and approval.payload["head_branch"] == "arena/a1b2c3"
                    approval_id = approval.id

                # 3. owner approves in the Approval Center -> gh pr merge --squash -> session closed
                r = await client.post(f"/api/v1/approvals/{approval_id}/decide", headers=headers,
                                      json={"decision": "approved", "review_note": "ship it"})
                assert r.status_code == 200, r.text
                assert any(c[1:3] == ["pr", "merge"] and "--squash" in c for c in gh.calls)
                async with env() as db:
                    assert (await db.get(Task, t1_id)).status == "done"
                    session = (await db.execute(select(ArenaSession).where(ArenaSession.task_id == t1_id))).scalar_one()
                    assert session.phase == "merged" and not session.is_open
                    assert "worker-1" not in await arena_reservations(db)
                    t2 = Task(project_id=project_id, title="Add greet()", description="Create greet().", status="queued",
                              execution_mode="manual", assigned_agent_id="worker-1", division_code="D02")
                    db.add(t2)
                    await db.commit()
                    t2_id = t2.id

                # 4. the next task starts with new_session
                async def _t2_submitted(db) -> bool:
                    s = (await db.execute(select(ArenaSession).where(ArenaSession.task_id == t2_id))).scalars().first()
                    return s is not None and s.submitted_at is not None

                await _drive(env, scheduler, loops, _t2_submitted)
                stats = await page.evaluate("() => window.__mockArena")
                assert stats["new_sessions"] == 2 and stats["submits"] == 3
                last_prompt = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('[data-message-author-role=user]')).map(e => e.textContent)")
                assert len(last_prompt) == 1 and f"[AATM-{t2_id}]" in last_prompt[0]
            finally:
                await browser.close()
                for loop in loops:
                    await loop.stop()
