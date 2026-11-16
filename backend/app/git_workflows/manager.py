import re
import shutil
from collections.abc import Callable, Coroutine, Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.async_subprocess import run_exec
from app.core.config import get_settings
from app.github_client.client import (
    GitHubClient,
    github_client,
    is_protected_branch,
    normalize_branch_name,
    sanitize_cli_output,
    validate_branch_name,
    verify_approval,
)
from app.models import ApprovalRequest, AuditLog, GitWorkflowRecord
from app.runtime.sandbox import WorkspaceSandboxError, resolve_contained_path

PROTECTED_BRANCHES = {"main", "master", "production", "prod"}
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_COMMIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./~^-]{0,199}$")


class GitWorkflowManager:
    """Inspects real Git repository state, worktrees, branches, and enforces protected-branch approvals."""

    def __init__(self, client: GitHubClient | None = None) -> None:
        self.github_client: GitHubClient = client or github_client

    @staticmethod
    async def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
        result = await run_exec(["git", *args], cwd=cwd, timeout=10)
        return result.returncode, sanitize_cli_output(result.output)

    @staticmethod
    def _resolve_worktree_dir(
        agent_id: str | None,
        *,
        repo_path: str | Path | None = None,
        worktrees_root: str | Path | None = None,
    ) -> tuple[bool, str, Path, Path | None]:
        """Validate `agent_id` and resolve its isolated `data/worktrees/<agent_id>` path."""
        root = Path(repo_path).resolve() if repo_path else Path(get_settings().REPO_ROOT).resolve()
        wt_base = Path(worktrees_root).resolve() if worktrees_root else (root / "data" / "worktrees").resolve()

        raw_id = (agent_id or "").strip()
        if not raw_id:
            return False, "Agent ID is required for worktree containment.", root, None
        if (
            raw_id.startswith(".")
            or raw_id.startswith("-")
            or ".." in raw_id
            or "/" in raw_id
            or "\\" in raw_id
            or not _AGENT_ID_RE.fullmatch(raw_id)
        ):
            return (
                False,
                f"Invalid agent_id '{sanitize_cli_output(raw_id)}' for worktree containment.",
                root,
                None,
            )

        wt_base.mkdir(parents=True, exist_ok=True)
        try:
            target_dir = resolve_contained_path(wt_base, raw_id)
        except WorkspaceSandboxError as exc:
            return False, sanitize_cli_output(str(exc)), root, None

        return True, raw_id, root, target_dir

    async def inspect_repo(self, repo_path: str | None = None) -> dict[str, Any]:
        root = Path(repo_path) if repo_path else get_settings().REPO_ROOT
        if not (root / ".git").exists():
            return {"git_available": False, "branch": "unknown", "worktrees": [], "recent_commits": [], "dirty_files": []}

        _, branch = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
        _, status_out = await self._run_git(["status", "--short"], root)
        _, log_out = await self._run_git(["log", "-n", "8", "--oneline"], root)
        _, wt_out = await self._run_git(["worktree", "list", "--porcelain"], root)

        worktrees: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in wt_out.splitlines():
            if not line.strip():
                if current:
                    worktrees.append(current)
                    current = {}
            elif " " in line:
                k, v = line.split(" ", 1)
                current[k] = v
        if current:
            worktrees.append(current)

        return {
            "git_available": True,
            "repo_path": str(root),
            "current_branch": branch,
            "is_protected": is_protected_branch(branch, PROTECTED_BRANCHES),
            "dirty_files": [ln for ln in status_out.splitlines() if ln.strip()],
            "recent_commits": [ln for ln in log_out.splitlines() if ln.strip()],
            "worktrees": worktrees,
        }

    async def create_agent_worktree(
        self,
        agent_id: str,
        branch_name: str,
        base_ref: str = "HEAD",
        db: AsyncSession | None = None,
        *,
        repo_path: str | Path | None = None,
        worktrees_root: str | Path | None = None,
        approval: ApprovalRequest | dict[str, Any] | None = None,
        actor: str = "worker-4",
    ) -> dict[str, Any]:
        """Create an isolated per-agent git worktree under `data/worktrees/<sanitized_agent_id>/`."""
        ok_agent, agent_or_err, root, target_dir = self._resolve_worktree_dir(
            agent_id,
            repo_path=repo_path,
            worktrees_root=worktrees_root,
        )
        if not ok_agent or target_dir is None:
            result = {
                "ok": False,
                "allowed": False,
                "agent_id": sanitize_cli_output(agent_id),
                "branch_name": sanitize_cli_output(branch_name),
                "error": agent_or_err,
                "reason": agent_or_err,
            }
            return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_blocked", agent_id, result))

        clean_agent_id = agent_or_err
        ok_branch, branch_or_err = validate_branch_name(branch_name)
        if not ok_branch:
            result = {
                "ok": False,
                "allowed": False,
                "agent_id": clean_agent_id,
                "branch_name": sanitize_cli_output(branch_name),
                "worktree_path": str(target_dir),
                "error": branch_or_err,
                "reason": branch_or_err,
            }
            return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result))
        clean_branch = branch_or_err

        protected_target = is_protected_branch(clean_branch, PROTECTED_BRANCHES)
        if protected_target or approval is not None:
            approved, reason, approval_id = verify_approval(
                approval,
                target_branch=clean_branch if protected_target else None,
            )
            if not approved:
                result = {
                    "ok": False,
                    "allowed": False,
                    "agent_id": clean_agent_id,
                    "branch_name": clean_branch,
                    "worktree_path": str(target_dir),
                    "is_protected_target": protected_target,
                    "approval_id": approval_id,
                    "error": reason,
                    "reason": reason,
                }
                return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result))

        clean_base_ref = (base_ref or "HEAD").strip()
        if (
            not clean_base_ref
            or clean_base_ref.startswith("-")
            or ".." in clean_base_ref
            or any(ch.isspace() for ch in clean_base_ref)
            or not _COMMIT_REF_RE.fullmatch(clean_base_ref)
        ):
            msg = f"Invalid base_ref '{sanitize_cli_output(clean_base_ref)}'."
            result = {
                "ok": False,
                "allowed": False,
                "agent_id": clean_agent_id,
                "branch_name": clean_branch,
                "worktree_path": str(target_dir),
                "error": msg,
                "reason": msg,
            }
            return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result))

        if not (root / ".git").exists():
            msg = f"Git repository not found at '{sanitize_cli_output(str(root))}'."
            result = {
                "ok": False,
                "allowed": False,
                "agent_id": clean_agent_id,
                "branch_name": clean_branch,
                "worktree_path": str(target_dir),
                "error": msg,
                "reason": msg,
            }
            return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result))

        # Prevent worktree directory or branch conflicts across agents.
        if target_dir.exists() and any(target_dir.iterdir()):
            msg = f"Worktree directory already exists for agent '{clean_agent_id}'."
            result = {
                "ok": False,
                "allowed": False,
                "agent_id": clean_agent_id,
                "branch_name": clean_branch,
                "worktree_path": str(target_dir),
                "error": msg,
                "reason": msg,
            }
            return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result))

        info = await self.inspect_repo(str(root))
        target_ref = f"refs/heads/{clean_branch}"
        for wt in info.get("worktrees", []):
            if wt.get("branch") == target_ref or Path(wt.get("worktree", "")).resolve() == target_dir:
                msg = f"Branch '{clean_branch}' or worktree path is already active in another worktree."
                result = {
                    "ok": False,
                    "allowed": False,
                    "agent_id": clean_agent_id,
                    "branch_name": clean_branch,
                    "worktree_path": str(target_dir),
                    "error": msg,
                    "reason": msg,
                }
                return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result))

        rc_ref, _ = await self._run_git(["show-ref", "--verify", "--quiet", target_ref], root)
        if rc_ref == 0:
            rc, out = await self._run_git(["worktree", "add", str(target_dir), clean_branch], root)
        else:
            rc, out = await self._run_git(
                ["worktree", "add", "-b", clean_branch, str(target_dir), clean_base_ref],
                root,
            )

        if rc != 0:
            result = {
                "ok": False,
                "allowed": True,
                "agent_id": clean_agent_id,
                "branch_name": clean_branch,
                "base_ref": clean_base_ref,
                "worktree_path": str(target_dir),
                "error": out,
                "reason": out,
            }
            return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result))

        result = {
            "ok": True,
            "allowed": True,
            "agent_id": clean_agent_id,
            "branch_name": clean_branch,
            "base_ref": clean_base_ref,
            "worktree_path": str(target_dir),
            "is_protected_target": protected_target,
            "output": out,
        }
        return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_create", clean_agent_id, result))

    async def remove_agent_worktree(
        self,
        agent_id: str,
        db: AsyncSession | None = None,
        *,
        repo_path: str | Path | None = None,
        worktrees_root: str | Path | None = None,
        force: bool = True,
        actor: str = "worker-4",
    ) -> dict[str, Any]:
        """Remove an agent's isolated git worktree under `data/worktrees/<sanitized_agent_id>/` and prune stale metadata."""
        ok_agent, agent_or_err, root, target_dir = self._resolve_worktree_dir(
            agent_id,
            repo_path=repo_path,
            worktrees_root=worktrees_root,
        )
        if not ok_agent or target_dir is None:
            result = {
                "ok": False,
                "allowed": False,
                "agent_id": sanitize_cli_output(agent_id),
                "error": agent_or_err,
                "reason": agent_or_err,
            }
            return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_remove_blocked", agent_id, result))

        clean_agent_id = agent_or_err
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(target_dir))

        rc, out = (0, "")
        if (root / ".git").exists() and target_dir.exists():
            rc, out = await self._run_git(args, root)
            await self._run_git(["worktree", "prune"], root)

        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
            if (root / ".git").exists():
                await self._run_git(["worktree", "prune"], root)

        removed = not target_dir.exists()
        result = {
            "ok": removed,
            "allowed": True,
            "agent_id": clean_agent_id,
            "worktree_path": str(target_dir),
            "removed": removed,
            "output": out,
            **({"error": out, "reason": out} if not removed and rc != 0 else {}),
        }
        return await self._finish(result, self._audit_worktree(db, actor, "git.worktree_remove", clean_agent_id, result))

    @staticmethod
    async def _finish(
        result: dict[str, Any],
        audit: Callable[[], Coroutine[Any, Any, dict[str, Any]]] | None,
    ) -> dict[str, Any]:
        if audit is not None:
            return await audit()
        return dict(result)

    @staticmethod
    def _audit_worktree(
        db: AsyncSession | None,
        actor: str,
        action: str,
        resource_id: str | None,
        result: dict[str, Any],
    ) -> Callable[[], Coroutine[Any, Any, dict[str, Any]]] | None:
        if db is None:
            return None

        async def _record() -> dict[str, Any]:
            db.add(
                AuditLog(
                    actor=actor,
                    action=action,
                    resource_type="git_worktree",
                    resource_id=str(resource_id or "")[:64],
                    details={
                        "agent_id": result.get("agent_id"),
                        "branch_name": result.get("branch_name"),
                        "worktree_path": result.get("worktree_path"),
                        "ok": result.get("ok"),
                        "allowed": result.get("allowed"),
                    },
                    severity="info" if result.get("ok") else "warning",
                )
            )
            await db.commit()
            return dict(result)

        return _record

    async def _resolve_approval(
        self,
        approval: ApprovalRequest | dict[str, Any] | None,
        approval_id: str | None,
        db: AsyncSession | None,
    ) -> ApprovalRequest | dict[str, Any] | None:
        if approval is None and approval_id and db is not None:
            return await db.get(ApprovalRequest, approval_id)
        return approval

    @staticmethod
    async def _audit_github(
        db: AsyncSession | None,
        *,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str | None,
        details: dict[str, Any],
        ok: bool,
    ) -> None:
        if db is None:
            return
        db.add(
            AuditLog(
                actor=actor,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details,
                severity="info" if ok else "warning",
            )
        )
        await db.commit()

    async def create_branch(
        self,
        branch_name: str,
        base_branch: str = "main",
        repo: str = "oldman-088/AATM",
        approval: ApprovalRequest | dict[str, Any] | None = None,
        *,
        source_sha: str | None = None,
        allowed_repos: Iterable[str] | None = None,
        protected_branches: Iterable[str] | None = None,
        require_approval: bool = False,
        cwd: Path | None = None,
        db: AsyncSession | None = None,
        approval_id: str | None = None,
        actor: str = "worker-4",
    ) -> dict[str, Any]:
        """Approval-gated branch creation helper wrapping `gh` CLI."""
        res = await self.github_client.create_branch(
            branch_name=branch_name,
            base_branch=base_branch,
            repo=repo,
            approval=await self._resolve_approval(approval, approval_id, db),
            source_sha=source_sha,
            allowed_repos=allowed_repos,
            protected_branches=protected_branches or PROTECTED_BRANCHES,
            require_approval=require_approval,
            cwd=cwd,
        )
        await self._audit_github(
            db,
            actor=actor,
            action="git.branch_create" if res.get("ok") else "git.branch_blocked",
            resource_type="git_branch",
            resource_id=res.get("branch_name") or normalize_branch_name(branch_name),
            details={"repo": res.get("repo"), "allowed": res.get("allowed"), "ok": res.get("ok")},
            ok=bool(res.get("ok")),
        )
        return res

    async def create_pull_request(
        self,
        repo: str,
        title: str,
        head: str,
        base: str = "main",
        body: str = "",
        *,
        draft: bool = False,
        approval: ApprovalRequest | dict[str, Any] | None = None,
        allowed_repos: Iterable[str] | None = None,
        protected_branches: Iterable[str] | None = None,
        require_approval: bool = True,
        cwd: Path | None = None,
        db: AsyncSession | None = None,
        approval_id: str | None = None,
        actor: str = "worker-4",
    ) -> dict[str, Any]:
        """Approval-gated pull request creation helper wrapping `gh pr create`."""
        res = await self.github_client.create_pull_request(
            repo=repo,
            title=title,
            head=head,
            base=base,
            body=body,
            draft=draft,
            approval=await self._resolve_approval(approval, approval_id, db),
            allowed_repos=allowed_repos,
            protected_branches=protected_branches or PROTECTED_BRANCHES,
            require_approval=require_approval,
            cwd=cwd,
        )
        await self._audit_github(
            db,
            actor=actor,
            action="git.pr_create" if res.get("ok") else "git.pr_blocked",
            resource_type="pull_request",
            resource_id=res.get("head") or normalize_branch_name(head),
            details={"repo": res.get("repo"), "base": res.get("base"), "allowed": res.get("allowed"), "ok": res.get("ok")},
            ok=bool(res.get("ok")),
        )
        return res

    async def create_issue(
        self,
        repo: str,
        title: str,
        body: str = "",
        *,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        approval: ApprovalRequest | dict[str, Any] | None = None,
        allowed_repos: Iterable[str] | None = None,
        require_approval: bool = True,
        cwd: Path | None = None,
        db: AsyncSession | None = None,
        approval_id: str | None = None,
        actor: str = "worker-4",
    ) -> dict[str, Any]:
        """Approval-gated GitHub issue creation helper wrapping `gh issue create`."""
        res = await self.github_client.create_issue(
            repo=repo,
            title=title,
            body=body,
            labels=labels,
            assignees=assignees,
            approval=await self._resolve_approval(approval, approval_id, db),
            allowed_repos=allowed_repos,
            require_approval=require_approval,
            cwd=cwd,
        )
        await self._audit_github(
            db,
            actor=actor,
            action="git.issue_create" if res.get("ok") else "git.issue_blocked",
            resource_type="github_issue",
            resource_id=res.get("repo"),
            details={"repo": res.get("repo"), "allowed": res.get("allowed"), "ok": res.get("ok")},
            ok=bool(res.get("ok")),
        )
        return res

    async def request_protected_merge(
        self,
        db: AsyncSession,
        record: GitWorkflowRecord,
        actor: str,
        approval: ApprovalRequest | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = normalize_branch_name(record.base_branch).lower()
        if target in PROTECTED_BRANCHES:
            has_valid_approval = False
            if approval is not None:
                has_valid_approval, _reason, _aid = verify_approval(approval, target_branch=record.base_branch)
            else:
                approved_res = await db.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.action_type == "merge",
                        ApprovalRequest.status == "approved",
                        ApprovalRequest.project_id == record.project_id,
                    )
                )
                has_valid_approval = approved_res.scalars().first() is not None

            if not has_valid_approval:
                req = ApprovalRequest(
                    project_id=record.project_id,
                    action_type="merge",
                    title=f"Protected Merge Authorization: {record.branch_name} -> {record.base_branch}",
                    description="Protected branch policy prevents automated merge without explicit user authorization.",
                    requested_by=actor,
                    payload={"workflow_id": record.id, "source": record.branch_name, "target": record.base_branch},
                    status="pending",
                )
                db.add(req)
                db.add(
                    AuditLog(
                        actor=actor,
                        action="git.merge_blocked",
                        resource_type="git_workflow",
                        resource_id=record.id,
                        details={"target_branch": record.base_branch, "approval_id": req.id},
                        severity="warning",
                    )
                )
                await db.commit()
                return {
                    "allowed": False,
                    "reason": f"Target branch '{record.base_branch}' is protected. Approval request '{req.id}' created.",
                    "approval_id": req.id,
                }

        record.pr_status = "ready_to_merge"
        record.review_status = "approved"
        await db.commit()
        return {"allowed": True, "reason": "Authorized for merge."}

    @staticmethod
    async def _exec(cmd: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
        """Run an argv-only command asynchronously without shell expansion, scrubbing credentials from output."""
        result = await run_exec(cmd, cwd=cwd, timeout=timeout)
        return (
            result.returncode,
            sanitize_cli_output(result.stdout.strip()),
            sanitize_cli_output(result.stderr.strip()),
        )

    @staticmethod
    def task_branch_name(task_id: str, title: str) -> str:
        """Build a safe `aatm/<task-id-short>-<slug>` branch name for a task worktree."""
        short_id = re.sub(r"[^a-zA-Z0-9]", "", (task_id or "").strip())[:8].lower() or "task"
        slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:36] or "work"
        candidate = f"aatm/{short_id}-{slug}"
        ok, cleaned = validate_branch_name(candidate)
        return cleaned if ok else f"aatm/{short_id}-work"

    async def create_task_worktree(
        self,
        task_id: str,
        title: str,
        *,
        repo_path: str | Path,
        base_branch: str = "main",
        worktrees_root: str | Path | None = None,
        db: AsyncSession | None = None,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        """Create (or reuse) an isolated per-task git worktree at `data/worktrees/task-<short>/` on `aatm/<short>-<slug>`."""
        root = Path(repo_path).resolve()
        if not (root / ".git").exists():
            return {
                "ok": False,
                "error": f"Git repository not found at '{sanitize_cli_output(str(root))}'.",
            }

        clean_task_id = re.sub(r"[^a-zA-Z0-9_-]", "", (task_id or "").strip())
        short_id = re.sub(r"[^a-zA-Z0-9]", "", clean_task_id)[:8].lower()
        if not short_id:
            return {"ok": False, "error": "Invalid task_id for worktree containment."}

        branch_name = self.task_branch_name(clean_task_id, title)
        clean_base = normalize_branch_name(base_branch) or "HEAD"
        if not _COMMIT_REF_RE.fullmatch(clean_base) or clean_base.startswith("-") or ".." in clean_base:
            clean_base = "HEAD"

        wt_base = Path(worktrees_root).resolve() if worktrees_root else (root / "data" / "worktrees").resolve()
        wt_base.mkdir(parents=True, exist_ok=True)
        try:
            target_dir = resolve_contained_path(wt_base, f"task-{short_id}")
        except WorkspaceSandboxError as exc:
            return {"ok": False, "error": sanitize_cli_output(str(exc))}

        if (target_dir / ".git").exists():
            rc_cur, cur_branch, _ = await self._exec(["git", "rev-parse", "--abbrev-ref", "HEAD"], target_dir)
            if rc_cur == 0 and cur_branch:
                return {
                    "ok": True,
                    "reused": True,
                    "task_id": task_id,
                    "branch_name": cur_branch,
                    "base_branch": clean_base,
                    "worktree_path": str(target_dir),
                }

        rc_base, _, _ = await self._exec(["git", "rev-parse", "--verify", clean_base], root)
        if rc_base != 0:
            clean_base = "HEAD"

        rc_ref, _, _ = await self._exec(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], root)
        if rc_ref == 0:
            rc, out, err = await self._exec(["git", "worktree", "add", str(target_dir), branch_name], root)
        else:
            rc, out, err = await self._exec(
                ["git", "worktree", "add", "-b", branch_name, str(target_dir), clean_base],
                root,
            )

        if rc != 0:
            return {
                "ok": False,
                "task_id": task_id,
                "branch_name": branch_name,
                "worktree_path": str(target_dir),
                "error": err or out or "Failed to create task git worktree.",
            }

        if db is not None:
            db.add(
                AuditLog(
                    actor=actor,
                    action="git.task_worktree_create",
                    resource_type="git_worktree",
                    resource_id=str(task_id)[:64],
                    details={"branch_name": branch_name, "worktree_path": str(target_dir), "base_branch": clean_base},
                )
            )
            await db.flush()

        return {
            "ok": True,
            "reused": False,
            "task_id": task_id,
            "branch_name": branch_name,
            "base_branch": clean_base,
            "worktree_path": str(target_dir),
        }

    async def commit_task_worktree(
        self,
        worktree_path: str | Path,
        *,
        agent_name: str,
        task_id: str,
        task_title: str,
    ) -> dict[str, Any]:
        """Stage and commit task worktree edits with `AATM(<agent>): <title>` and `AATM-Task: <id>` trailer."""
        wt = Path(worktree_path).resolve()
        if not wt.is_dir() or not (wt / ".git").exists():
            return {"ok": False, "committed": False, "error": "Task worktree path is not a git worktree."}

        rc_status, status_out, err_status = await self._exec(["git", "status", "--porcelain"], wt)
        if rc_status != 0:
            return {"ok": False, "committed": False, "error": err_status or "git status failed."}
        if not status_out.strip():
            rc_head, sha, _ = await self._exec(["git", "rev-parse", "HEAD"], wt)
            return {
                "ok": True,
                "committed": False,
                "commit_sha": sha if rc_head == 0 else None,
                "reason": "No uncommitted changes in worktree.",
            }

        rc_add, _, err_add = await self._exec(["git", "add", "-A"], wt)
        if rc_add != 0:
            return {"ok": False, "committed": False, "error": err_add or "git add failed."}

        clean_agent = re.sub(r"[\r\n]+", " ", (agent_name or "worker").strip())[:48]
        clean_title = re.sub(r"[\r\n]+", " ", (task_title or "Task update").strip())[:100]
        subject = f"AATM({clean_agent}): {clean_title}"
        trailer = f"AATM-Task: {task_id}"

        rc_commit, out_commit, err_commit = await self._exec(
            [
                "git",
                "-c",
                "user.name=AATM Agent",
                "-c",
                "user.email=aatm-agent@local",
                "commit",
                "-m",
                subject,
                "-m",
                trailer,
            ],
            wt,
        )
        if rc_commit != 0:
            return {"ok": False, "committed": False, "error": err_commit or out_commit or "git commit failed."}

        rc_sha, sha, _ = await self._exec(["git", "rev-parse", "HEAD"], wt)
        return {
            "ok": True,
            "committed": True,
            "commit_sha": sha if rc_sha == 0 else None,
            "subject": subject,
            "trailer": trailer,
        }

    async def push_and_create_draft_pr(
        self,
        worktree_path: str | Path,
        *,
        branch_name: str,
        base_branch: str = "main",
        title: str,
        body: str,
        repo_slug: str | None = None,
    ) -> dict[str, Any]:
        """Push task branch to `origin` and open a draft pull request via `gh pr create --draft`."""
        wt = Path(worktree_path).resolve()
        ok_br, clean_branch = validate_branch_name(branch_name)
        if not ok_br:
            return {"ok": False, "skipped": False, "error": clean_branch}
        if is_protected_branch(clean_branch, PROTECTED_BRANCHES):
            return {"ok": False, "skipped": False, "error": f"Refusing to push protected branch '{clean_branch}'."}

        rc_remote, remote_url, _ = await self._exec(["git", "remote", "get-url", "origin"], wt)
        if rc_remote != 0 or not remote_url:
            return {"ok": False, "skipped": True, "reason": "No origin remote configured; skipping push and draft PR."}

        rc_push, out_push, err_push = await self._exec(["git", "push", "-u", "origin", clean_branch], wt, timeout=60)
        if rc_push != 0:
            return {"ok": False, "skipped": False, "error": err_push or out_push or "git push failed."}

        clean_base = normalize_branch_name(base_branch) or "main"
        cmd = [
            "gh",
            "pr",
            "create",
            "--draft",
            "--head",
            clean_branch,
            "--base",
            clean_base,
            "--title",
            re.sub(r"[\r\n]+", " ", title.strip())[:180],
            "--body",
            body.strip()[:4000],
        ]
        if repo_slug and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_slug.strip()):
            cmd.extend(["--repo", repo_slug.strip()])

        rc_pr, out_pr, err_pr = await self._exec(cmd, wt, timeout=60)
        if rc_pr != 0:
            return {"ok": False, "skipped": False, "pushed": True, "error": err_pr or out_pr or "gh pr create failed."}

        pr_match = re.search(r"https://[^\s]+/pull/(\d+)", out_pr)
        pr_url = pr_match.group(0) if pr_match else out_pr.strip()
        pr_number = int(pr_match.group(1)) if pr_match else None
        return {
            "ok": True,
            "pushed": True,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "pr_status": "draft",
        }

    @staticmethod
    def repo_slug_from_url(repo_url: str | None) -> str | None:
        """Extract `owner/name` from a GitHub URL; None for non-GitHub or missing URLs."""
        if not repo_url or "github.com" not in repo_url:
            return None
        match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", repo_url.strip())
        return match.group(1) if match else None

    async def branch_diff(self, worktree_path: str | Path, base_branch: str, *, max_chars: int = 20_000) -> dict[str, Any]:
        """Return `git diff <base>...HEAD` for a task worktree (read-only, sanitized, truncated)."""
        wt = Path(worktree_path).resolve()
        if not wt.is_dir():
            return {"ok": False, "error": "Task worktree is not available.", "diff": "", "files": []}
        clean_base = normalize_branch_name(base_branch) or "main"
        if not _COMMIT_REF_RE.fullmatch(clean_base) or clean_base.startswith("-"):
            return {"ok": False, "error": "Invalid base branch.", "diff": "", "files": []}
        rc, diff, err = await self._exec(["git", "diff", "--no-color", "--no-ext-diff", f"{clean_base}...HEAD"], wt)
        if rc != 0:
            return {"ok": False, "error": err or "git diff failed.", "diff": "", "files": []}
        rc_names, names, _ = await self._exec(["git", "diff", "--name-only", f"{clean_base}...HEAD"], wt)
        files = [line for line in names.splitlines() if line.strip()] if rc_names == 0 else []
        truncated = len(diff) > max_chars
        return {"ok": True, "diff": diff[:max_chars], "truncated": truncated, "files": files}

    async def push_task_branch(self, worktree_path: str | Path, *, branch_name: str) -> dict[str, Any]:
        """Push follow-up commits on an existing (non-protected) task branch; no new PR is opened."""
        wt = Path(worktree_path).resolve()
        ok_br, clean_branch = validate_branch_name(branch_name)
        if not ok_br:
            return {"ok": False, "error": clean_branch}
        if is_protected_branch(clean_branch, PROTECTED_BRANCHES):
            return {"ok": False, "error": f"Refusing to push protected branch '{clean_branch}'."}
        rc_remote, remote_url, _ = await self._exec(["git", "remote", "get-url", "origin"], wt)
        if rc_remote != 0 or not remote_url:
            return {"ok": False, "skipped": True, "reason": "No origin remote configured; skipping push."}
        rc_push, out_push, err_push = await self._exec(["git", "push", "origin", clean_branch], wt, timeout=60)
        if rc_push != 0:
            return {"ok": False, "error": err_push or out_push or "git push failed."}
        return {"ok": True, "pushed": True, "branch_name": clean_branch}

    async def post_pr_review(
        self,
        *,
        repo_slug: str,
        pr_number: int,
        event: str,
        body: str,
        cwd: str | Path,
    ) -> dict[str, Any]:
        """Post the AI reviewer's verdict on an AATM-opened PR (COMMENT or REQUEST_CHANGES; never APPROVE).

        GitHub rejects REQUEST_CHANGES on a PR opened by the same account (AATM uses the host's
        `gh` auth for both), so that case falls back to a COMMENT review carrying the verdict.
        """
        clean_event = str(event or "").strip().upper()
        if clean_event not in {"COMMENT", "REQUEST_CHANGES"}:
            return {"ok": False, "error": "AI reviewers may only COMMENT or REQUEST_CHANGES."}
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_slug or ""):
            return {"ok": False, "error": "Invalid repository slug."}
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            return {"ok": False, "error": "Pull request number must be a positive integer."}
        clean_body = sanitize_cli_output(body.strip())[:8_000]
        flag = "--comment" if clean_event == "COMMENT" else "--request-changes"
        base_cmd = ["gh", "pr", "review", str(pr_number), "--repo", repo_slug]
        rc, out, err = await self._exec([*base_cmd, flag, "--body", clean_body], Path(cwd), timeout=60)
        if rc == 0:
            return {"ok": True, "posted_event": clean_event, "fallback": False, "output": out}
        if clean_event == "REQUEST_CHANGES":
            verdict_body = f"**AATM Reviewer verdict: CHANGES REQUESTED**\n\n{clean_body}"
            rc2, out2, err2 = await self._exec([*base_cmd, "--comment", "--body", verdict_body], Path(cwd), timeout=60)
            if rc2 == 0:
                return {
                    "ok": True,
                    "posted_event": "COMMENT",
                    "fallback": True,
                    "fallback_reason": err or out or "request-changes rejected",
                    "output": out2,
                }
            return {"ok": False, "error": err2 or out2 or err or "gh pr review failed."}
        return {"ok": False, "error": err or out or "gh pr review failed."}

    async def merge_pull_request(self, *, repo_slug: str, pr_number: int, cwd: str | Path) -> dict[str, Any]:
        """Mark a draft PR ready and squash-merge it with `gh pr merge --squash` (caller enforces approval)."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_slug or ""):
            return {"ok": False, "error": "Invalid repository slug."}
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            return {"ok": False, "error": "Pull request number must be a positive integer."}
        work = Path(cwd)
        rc_ready, out_ready, err_ready = await self._exec(
            ["gh", "pr", "ready", str(pr_number), "--repo", repo_slug], work, timeout=60
        )
        rc, out, err = await self._exec(
            ["gh", "pr", "merge", str(pr_number), "--repo", repo_slug, "--squash"], work, timeout=120
        )
        if rc != 0:
            return {
                "ok": False,
                "error": err or out or "gh pr merge failed.",
                "ready_output": out_ready or err_ready,
            }
        return {"ok": True, "merged": True, "ready_rc": rc_ready, "output": out}

    async def cleanup_task_worktree(
        self,
        *,
        repo_path: str | Path,
        worktree_path: str | Path | None = None,
        branch_name: str | None = None,
        delete_remote_branch: bool = False,
        db: AsyncSession | None = None,
        task_id: str | None = None,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        """Remove a task git worktree and delete its non-protected task branch after merge or cancellation."""
        root = Path(repo_path).resolve()
        wt_removed = False
        branch_deleted = False
        remote_deleted = False

        if worktree_path:
            wt = Path(worktree_path).resolve()
            wt_root = (root / "data" / "worktrees").resolve()
            if wt.exists():
                if (root / ".git").exists():
                    await self._exec(["git", "worktree", "remove", "--force", str(wt)], root)
                    await self._exec(["git", "worktree", "prune"], root)
                if wt.exists() and wt_root in wt.parents:
                    shutil.rmtree(wt, ignore_errors=True)
                    if (root / ".git").exists():
                        await self._exec(["git", "worktree", "prune"], root)
            wt_removed = not wt.exists()

        if branch_name and (root / ".git").exists():
            ok_br, clean_branch = validate_branch_name(branch_name)
            if ok_br and not is_protected_branch(clean_branch, PROTECTED_BRANCHES):
                rc_ref, _, _ = await self._exec(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{clean_branch}"], root)
                if rc_ref == 0:
                    rc_del, _, _ = await self._exec(["git", "branch", "-D", clean_branch], root)
                    branch_deleted = rc_del == 0
                if delete_remote_branch:
                    rc_rem, _, _ = await self._exec(["git", "remote", "get-url", "origin"], root)
                    if rc_rem == 0:
                        rc_rdel, _, _ = await self._exec(["git", "push", "origin", "--delete", clean_branch], root, timeout=30)
                        remote_deleted = rc_rdel == 0

        if db is not None:
            db.add(
                AuditLog(
                    actor=actor,
                    action="git.task_worktree_cleanup",
                    resource_type="git_worktree",
                    resource_id=str(task_id or branch_name or "")[:64],
                    details={
                        "worktree_removed": wt_removed,
                        "branch_deleted": branch_deleted,
                        "remote_deleted": remote_deleted,
                        "branch_name": branch_name,
                    },
                )
            )
            await db.flush()

        return {
            "ok": True,
            "worktree_removed": wt_removed,
            "branch_deleted": branch_deleted,
            "remote_deleted": remote_deleted,
        }


git_workflow_manager = GitWorkflowManager()
