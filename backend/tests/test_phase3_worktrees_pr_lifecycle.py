"""Phase 3 verification tests: per-task git worktrees, .git sandbox blocking, auto-commit, draft PRs, and webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.git_workflows.manager import git_workflow_manager
from app.main import app
from app.models import Agent, AgentMessage, AIProvider, GitWorkflowRecord, Project, Task, User
from app.providers.base import ProviderRequest, ProviderResponse
from app.runtime.agent_loop import AgentLoop
from app.runtime.agent_protocol import ActionRejected, safe_workspace_path, validate_run_command


class _ScriptedRouter:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[ProviderRequest] = []

    async def route_and_execute(
        self,
        db: AsyncSession,
        request: ProviderRequest,
        preferred_provider_id: str | None = None,
    ) -> ProviderResponse:
        self.calls.append(request)
        if not self._replies:
            raise AssertionError("ScriptedRouter exhausted")
        return ProviderResponse(
            success=True,
            provider_id=preferred_provider_id or "anthropic-claude",
            model="claude-3-7-sonnet-latest",
            content=self._replies.pop(0),
            input_tokens=15,
            output_tokens=10,
            estimated_cost_usd=0.001,
            pricing_known=True,
            latency_ms=5.0,
        )


def _init_git_repo(repo_dir: Path, *, with_bare_origin: Path | None = None) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AATM Test"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@aatm.local"], cwd=str(repo_dir), check=True, capture_output=True)
    (repo_dir / "README.md").write_text("# Sample Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)
    if with_bare_origin is not None:
        subprocess.run(["git", "init", "--bare", str(with_bare_origin)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(with_bare_origin)], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(repo_dir), check=True, capture_output=True)


@pytest.fixture()
async def phase3_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "phase3.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setenv("TEST_CLAUDE_KEY", "sk-ant-phase3-dummy")

    async with session_factory() as db:
        await seed_initial_data(db)
        prov = await db.get(AIProvider, "anthropic-claude")
        assert prov is not None
        prov.is_enabled = True
        prov.credential_ref = "env:TEST_CLAUDE_KEY"
        await db.commit()

    yield session_factory
    await engine.dispose()


async def test_phase3_two_concurrent_task_worktrees_blocks_dotgit_and_commits_and_opens_draft_pr(
    phase3_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo_dir = tmp_path / "sample-repo"
    bare_origin = tmp_path / "origin.git"
    _init_git_repo(repo_dir, with_bare_origin=bare_origin)

    # Stub gh pr create so push runs against real bare git origin while PR creation returns a draft PR URL.
    orig_exec = git_workflow_manager._exec
    gh_calls: list[list[str]] = []

    async def _mock_exec(cmd: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
        if cmd[:3] == ["gh", "pr", "create"]:
            gh_calls.append(list(cmd))
            return 0, "https://github.com/oldman-088/AATM/pull/42", ""
        return await orig_exec(cmd, cwd, timeout=timeout)

    monkeypatch.setattr(git_workflow_manager, "_exec", _mock_exec)

    async with phase3_db() as db:
        proj = Project(
            name="Worktree Project",
            slug="worktree-proj",
            description="Phase 3 git worktree project",
            repo_path=str(repo_dir),
            repo_url="https://github.com/oldman-088/AATM",
            default_branch="main",
            config={"worktree_mode": True, "auto_pr": True},
        )
        db.add(proj)
        await db.flush()

        w1 = await db.get(Agent, "worker-1")
        w2 = await db.get(Agent, "worker-2")
        assert w1 is not None and w2 is not None
        w1.provider_id = "anthropic-claude"
        w2.provider_id = "anthropic-claude"

        t1 = Task(
            project_id=proj.id,
            title="Implement math helper",
            description="Create add_nums.py and test_add_nums.py",
            status="in_progress",
            assigned_agent_id=w1.id,
            expected_deliverables=["add_nums.py", "test_add_nums.py"],
            lock_owner=w1.id,
        )
        t2 = Task(
            project_id=proj.id,
            title="Implement string helper",
            description="Create slugify.py and test_slugify.py",
            status="in_progress",
            assigned_agent_id=w2.id,
            expected_deliverables=["slugify.py", "test_slugify.py"],
            lock_owner=w2.id,
        )
        db.add_all([t1, t2])
        await db.flush()

        m1 = AgentMessage(
            task_id=t1.id,
            project_id=proj.id,
            sender_id="manager-0",
            recipient_id=w1.id,
            channel_type="task",
            instructions="Create add_nums.py and test_add_nums.py",
            execution_status="running",
            delivery_status="acknowledged",
            permissions=w1.permissions,
        )
        m2 = AgentMessage(
            task_id=t2.id,
            project_id=proj.id,
            sender_id="manager-0",
            recipient_id=w2.id,
            channel_type="task",
            instructions="Create slugify.py and test_slugify.py",
            execution_status="running",
            delivery_status="acknowledged",
            permissions=w2.permissions,
        )
        db.add_all([m1, m2])
        await db.commit()

    router1 = _ScriptedRouter(
        [
            '{"action":"read","path":".git"}',  # must be rejected by sandbox!
            '{"action":"write","path":"add_nums.py","content":"def add_nums(a, b):\\n    return a + b\\n"}',
            '{"action":"write","path":"test_add_nums.py","content":"from add_nums import add_nums\\n\\ndef test_add():\\n    assert add_nums(2, 3) == 5\\n"}',
            '{"action":"run","command":["pytest","-q","test_add_nums.py"]}',
            '{"action":"done","summary":"Implemented add_nums and passed tests.","deliverables":["add_nums.py","test_add_nums.py"]}',
        ]
    )
    router2 = _ScriptedRouter(
        [
            '{"action":"write","path":"slugify.py","content":"def slugify(s):\\n    return s.lower().replace(\\" \\", \\"-\\")\\n"}',
            '{"action":"write","path":"test_slugify.py","content":"from slugify import slugify\\n\\ndef test_slug():\\n    assert slugify(\\"Hello World\\") == \\"hello-world\\"\\n"}',
            '{"action":"done","summary":"Implemented slugify.","deliverables":["slugify.py","test_slugify.py"]}',
        ]
    )

    loop1 = AgentLoop(session_factory=phase3_db, provider_router=router1, poll_seconds=0.01)
    loop2 = AgentLoop(session_factory=phase3_db, provider_router=router2, poll_seconds=0.01)

    # Process both tasks
    assert await loop1.process_one() is True
    assert await loop2.process_one() is True

    async with phase3_db() as db:
        t1_ref = await db.get(Task, t1.id)
        t2_ref = await db.get(Task, t2.id)
        m1_ref = await db.get(AgentMessage, m1.id)
        assert t1_ref is not None and t2_ref is not None and m1_ref is not None

        # Distinct worktree paths and aatm/<short>-<slug> branches
        assert t1_ref.worktree_path and t2_ref.worktree_path
        assert t1_ref.worktree_path != t2_ref.worktree_path
        assert t1_ref.branch_name and t1_ref.branch_name.startswith("aatm/")
        assert t2_ref.branch_name and t2_ref.branch_name.startswith("aatm/")
        assert t1_ref.branch_name != t2_ref.branch_name

        wt1 = Path(t1_ref.worktree_path)
        wt2 = Path(t2_ref.worktree_path)
        assert (wt1 / "add_nums.py").is_file()
        assert not (wt1 / "slugify.py").exists()
        assert (wt2 / "slugify.py").is_file()
        assert not (wt2 / "add_nums.py").exists()

        # .git is blocked by sandbox inside worktree
        with pytest.raises(ActionRejected):
            safe_workspace_path(wt1, ".git")
        with pytest.raises(ActionRejected):
            safe_workspace_path(wt1, ".git/HEAD")
        with pytest.raises(ActionRejected):
            validate_run_command(wt1, ["ruff", "check", ".git"])
        assert "rejected" in m1_ref.response_payload["iterations"][0]["result"].lower()

        # Auto-commit message format & AATM-Task trailer
        assert t1_ref.commit_sha is not None
        commit_msg = subprocess.run(
            ["git", "log", "-1", "--pretty=%B", t1_ref.commit_sha],
            cwd=str(wt1),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "AATM(" in commit_msg
        assert f"AATM-Task: {t1.id}" in commit_msg

        # Draft PR opened and task moved to pr_open
        assert t1_ref.status == "pr_open"
        assert t1_ref.pr_number == 42
        assert t1_ref.pr_url == "https://github.com/oldman-088/AATM/pull/42"
        assert t1_ref.pr_status == "draft"
        assert len(gh_calls) == 2
        assert "--draft" in gh_calls[0]

        # Origin received the pushed branch
        remote_branches = subprocess.run(
            ["git", "--git-dir", str(bare_origin), "branch"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert t1_ref.branch_name in remote_branches

        wf = (await db.execute(select(GitWorkflowRecord).where(GitWorkflowRecord.task_id == t1.id))).scalar_one()
        assert wf.pr_number == 42
        assert wf.last_commit_sha == t1_ref.commit_sha


async def test_phase3_github_webhook_lifecycle_and_post_merge_cleanup(
    phase3_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo_dir = tmp_path / "webhook-repo"
    _init_git_repo(repo_dir)

    settings = get_settings()
    webhook_secret = "phase3-webhook-signing-secret"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", webhook_secret)

    async def _override_db():
        async with phase3_db() as db:
            yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        async with phase3_db() as db:
            proj = Project(
                name="Webhook Project",
                slug="webhook-proj",
                repo_path=str(repo_dir),
                default_branch="main",
                config={"worktree_mode": True},
            )
            db.add(proj)
            await db.flush()

            task = Task(
                project_id=proj.id,
                title="Webhook lifecycle feature",
                status="pr_open",
                pr_number=108,
                pr_status="draft",
            )
            db.add(task)
            await db.flush()

            wt = await git_workflow_manager.create_task_worktree(
                task.id,
                task.title,
                repo_path=repo_dir,
                base_branch="main",
                db=db,
            )
            assert wt["ok"] is True
            task.branch_name = wt["branch_name"]
            task.worktree_path = wt["worktree_path"]
            db.add(
                GitWorkflowRecord(
                    project_id=proj.id,
                    task_id=task.id,
                    branch_name=task.branch_name,
                    worktree_path=task.worktree_path,
                    pr_number=108,
                    pr_status="draft",
                )
            )
            await db.commit()
            wt_path = Path(task.worktree_path)
            branch_name = task.branch_name
            assert wt_path.is_dir()

        def _post_signed(client: AsyncClient, event: str, payload: dict[str, Any]):
            raw = json.dumps(payload).encode("utf-8")
            sig = "sha256=" + hmac.new(webhook_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            return client.post(
                "/api/v1/github/webhooks",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": event,
                    "X-GitHub-Delivery": f"deliv-{event}-{payload.get('action', '')}",
                    "X-Hub-Signature-256": sig,
                },
            )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. ready_for_review -> in_review
            r1 = await _post_signed(
                client,
                "pull_request",
                {"action": "ready_for_review", "pull_request": {"number": 108, "head": {"ref": branch_name}}},
            )
            assert r1.status_code == 202
            assert r1.json()["task_status"] == "in_review"

            # 2. changes_requested
            r2 = await _post_signed(
                client,
                "pull_request_review",
                {
                    "action": "submitted",
                    "review": {"state": "changes_requested"},
                    "pull_request": {"number": 108, "head": {"ref": branch_name}},
                },
            )
            assert r2.status_code == 202
            assert r2.json()["task_status"] == "changes_requested"

            # 3. approved
            r3 = await _post_signed(
                client,
                "pull_request_review",
                {
                    "action": "submitted",
                    "review": {"state": "approved"},
                    "pull_request": {"number": 108, "head": {"ref": branch_name}},
                },
            )
            assert r3.status_code == 202
            assert r3.json()["task_status"] == "approved"

            # 4. closed + merged=True -> merged -> post-merge cleanup -> done
            r4 = await _post_signed(
                client,
                "pull_request",
                {
                    "action": "closed",
                    "pull_request": {"number": 108, "merged": True, "head": {"ref": branch_name}},
                },
            )
            assert r4.status_code == 202
            assert r4.json()["task_status"] == "done"

        async with phase3_db() as db:
            refreshed = await db.get(Task, task.id)
            assert refreshed is not None
            assert refreshed.status == "done"
            assert refreshed.pr_status == "merged"
            assert refreshed.worktree_path is None
            statuses_in_checkpoints = [c.get("status") for c in (refreshed.checkpoints or [])]
            assert "merged" in statuses_in_checkpoints
            assert "done" in statuses_in_checkpoints

        # Worktree directory and local task branch were removed; main branch remains intact.
        assert not wt_path.exists()
        branches_out = subprocess.run(["git", "branch"], cwd=str(repo_dir), capture_output=True, text=True, check=True).stdout
        assert branch_name not in branches_out
        assert "main" in branches_out
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_phase3_cancel_task_cleans_up_worktree_and_branch(
    phase3_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    repo_dir = tmp_path / "cancel-repo"
    _init_git_repo(repo_dir)

    async def _override_db():
        async with phase3_db() as db:
            yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        async with phase3_db() as db:
            admin = (await db.execute(select(User).where(User.role == "admin"))).scalar_one()
            admin.must_change_password = False
            proj = Project(name="Cancel Proj", slug="cancel-proj", repo_path=str(repo_dir), default_branch="main")
            db.add(proj)
            await db.flush()
            task = Task(project_id=proj.id, title="Cancelled worktree task", status="in_progress")
            db.add(task)
            await db.flush()
            wt = await git_workflow_manager.create_task_worktree(task.id, task.title, repo_path=repo_dir, db=db)
            task.branch_name = wt["branch_name"]
            task.worktree_path = wt["worktree_path"]
            await db.commit()
            token = create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)
            wt_path = Path(task.worktree_path)
            branch_name = task.branch_name

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                f"/api/v1/tasks/{task.id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"status": "cancelled"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "cancelled"
            assert resp.json()["worktree_path"] is None

        assert not wt_path.exists()
        branches_out = subprocess.run(["git", "branch"], cwd=str(repo_dir), capture_output=True, text=True, check=True).stdout
        assert branch_name not in branches_out
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_phase3_no_create_all_in_lifespan_and_alembic_in_start_scripts():
    repo_root = Path(__file__).resolve().parents[2]
    main_text = (repo_root / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert "Base.metadata.create_all" not in main_text

    ubuntu_start = (repo_root / "scripts" / "ubuntu" / "start.sh").read_text(encoding="utf-8")
    assert "alembic" in ubuntu_start and "upgrade head" in ubuntu_start

    win_start = (repo_root / "scripts" / "windows" / "start.ps1").read_text(encoding="utf-8")
    assert "alembic" in win_start and "upgrade head" in win_start
