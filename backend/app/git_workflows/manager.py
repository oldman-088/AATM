import re
import shutil
import subprocess
from collections.abc import Callable, Coroutine, Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


class _AwaitableResult(dict):
    """Dictionary result that can also be awaited when used inside async workflows."""

    def __init__(
        self,
        data: dict[str, Any],
        async_fn: Callable[[], Coroutine[Any, Any, dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(data)
        self._async_fn = async_fn

    def __await__(self):
        async def _resolve() -> dict[str, Any]:
            if self._async_fn is not None:
                updated = await self._async_fn()
                self.clear()
                self.update(updated)
            return dict(self)

        return _resolve().__await__()


class GitWorkflowManager:
    """Inspects real Git repository state, worktrees, branches, and enforces protected-branch approvals."""

    def __init__(self, client: GitHubClient | None = None) -> None:
        self.github_client: GitHubClient = client or github_client

    @staticmethod
    def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=10,
            )
            raw_out = (proc.stdout or proc.stderr or "").strip()
            return proc.returncode, sanitize_cli_output(raw_out)
        except Exception as exc:
            return 1, sanitize_cli_output(str(exc))

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

    def inspect_repo(self, repo_path: str | None = None) -> dict[str, Any]:
        root = Path(repo_path) if repo_path else get_settings().REPO_ROOT
        if not (root / ".git").exists():
            return {"git_available": False, "branch": "unknown", "worktrees": [], "recent_commits": [], "dirty_files": []}

        _, branch = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
        _, status_out = self._run_git(["status", "--short"], root)
        _, log_out = self._run_git(["log", "-n", "8", "--oneline"], root)
        _, wt_out = self._run_git(["worktree", "list", "--porcelain"], root)

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

    def create_agent_worktree(
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
    ) -> _AwaitableResult:
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
            return _AwaitableResult(result, self._audit_worktree(db, actor, "git.worktree_blocked", agent_id, result))

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
            return _AwaitableResult(
                result,
                self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result),
            )
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
                return _AwaitableResult(
                    result,
                    self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result),
                )

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
            return _AwaitableResult(
                result,
                self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result),
            )

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
            return _AwaitableResult(
                result,
                self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result),
            )

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
            return _AwaitableResult(
                result,
                self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result),
            )

        info = self.inspect_repo(str(root))
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
                return _AwaitableResult(
                    result,
                    self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result),
                )

        rc_ref, _ = self._run_git(["show-ref", "--verify", "--quiet", target_ref], root)
        if rc_ref == 0:
            rc, out = self._run_git(["worktree", "add", str(target_dir), clean_branch], root)
        else:
            rc, out = self._run_git(
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
            return _AwaitableResult(
                result,
                self._audit_worktree(db, actor, "git.worktree_blocked", clean_agent_id, result),
            )

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
        return _AwaitableResult(
            result,
            self._audit_worktree(db, actor, "git.worktree_create", clean_agent_id, result),
        )

    def remove_agent_worktree(
        self,
        agent_id: str,
        db: AsyncSession | None = None,
        *,
        repo_path: str | Path | None = None,
        worktrees_root: str | Path | None = None,
        force: bool = True,
        actor: str = "worker-4",
    ) -> _AwaitableResult:
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
            return _AwaitableResult(
                result,
                self._audit_worktree(db, actor, "git.worktree_remove_blocked", agent_id, result),
            )

        clean_agent_id = agent_or_err
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(target_dir))

        rc, out = (0, "")
        if (root / ".git").exists() and target_dir.exists():
            rc, out = self._run_git(args, root)
            self._run_git(["worktree", "prune"], root)

        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
            if (root / ".git").exists():
                self._run_git(["worktree", "prune"], root)

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
        return _AwaitableResult(
            result,
            self._audit_worktree(db, actor, "git.worktree_remove", clean_agent_id, result),
        )

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

    def create_branch(
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
    ) -> _AwaitableResult:
        """Approval-gated branch creation helper wrapping `gh` CLI."""
        result = self.github_client.create_branch(
            branch_name=branch_name,
            base_branch=base_branch,
            repo=repo,
            approval=approval,
            source_sha=source_sha,
            allowed_repos=allowed_repos,
            protected_branches=protected_branches or PROTECTED_BRANCHES,
            require_approval=require_approval,
            cwd=cwd,
        )

        async def _with_db() -> dict[str, Any]:
            resolved_approval = approval
            if resolved_approval is None and approval_id and db is not None:
                resolved_approval = await db.get(ApprovalRequest, approval_id)
                res = self.github_client.create_branch(
                    branch_name=branch_name,
                    base_branch=base_branch,
                    repo=repo,
                    approval=resolved_approval,
                    source_sha=source_sha,
                    allowed_repos=allowed_repos,
                    protected_branches=protected_branches or PROTECTED_BRANCHES,
                    require_approval=require_approval,
                    cwd=cwd,
                )
            else:
                res = dict(result)
            if db is not None:
                db.add(
                    AuditLog(
                        actor=actor,
                        action="git.branch_create" if res.get("ok") else "git.branch_blocked",
                        resource_type="git_branch",
                        resource_id=res.get("branch_name") or normalize_branch_name(branch_name),
                        details={"repo": res.get("repo"), "allowed": res.get("allowed"), "ok": res.get("ok")},
                        severity="info" if res.get("ok") else "warning",
                    )
                )
                await db.commit()
            return res

        return _AwaitableResult(result, _with_db if db is not None else None)

    def create_pull_request(
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
    ) -> _AwaitableResult:
        """Approval-gated pull request creation helper wrapping `gh pr create`."""
        result = self.github_client.create_pull_request(
            repo=repo,
            title=title,
            head=head,
            base=base,
            body=body,
            draft=draft,
            approval=approval,
            allowed_repos=allowed_repos,
            protected_branches=protected_branches or PROTECTED_BRANCHES,
            require_approval=require_approval,
            cwd=cwd,
        )

        async def _with_db() -> dict[str, Any]:
            resolved_approval = approval
            if resolved_approval is None and approval_id and db is not None:
                resolved_approval = await db.get(ApprovalRequest, approval_id)
                res = self.github_client.create_pull_request(
                    repo=repo,
                    title=title,
                    head=head,
                    base=base,
                    body=body,
                    draft=draft,
                    approval=resolved_approval,
                    allowed_repos=allowed_repos,
                    protected_branches=protected_branches or PROTECTED_BRANCHES,
                    require_approval=require_approval,
                    cwd=cwd,
                )
            else:
                res = dict(result)
            if db is not None:
                db.add(
                    AuditLog(
                        actor=actor,
                        action="git.pr_create" if res.get("ok") else "git.pr_blocked",
                        resource_type="pull_request",
                        resource_id=res.get("head") or normalize_branch_name(head),
                        details={
                            "repo": res.get("repo"),
                            "base": res.get("base"),
                            "allowed": res.get("allowed"),
                            "ok": res.get("ok"),
                        },
                        severity="info" if res.get("ok") else "warning",
                    )
                )
                await db.commit()
            return res

        return _AwaitableResult(result, _with_db if db is not None else None)

    def create_issue(
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
    ) -> _AwaitableResult:
        """Approval-gated GitHub issue creation helper wrapping `gh issue create`."""
        result = self.github_client.create_issue(
            repo=repo,
            title=title,
            body=body,
            labels=labels,
            assignees=assignees,
            approval=approval,
            allowed_repos=allowed_repos,
            require_approval=require_approval,
            cwd=cwd,
        )

        async def _with_db() -> dict[str, Any]:
            resolved_approval = approval
            if resolved_approval is None and approval_id and db is not None:
                resolved_approval = await db.get(ApprovalRequest, approval_id)
                res = self.github_client.create_issue(
                    repo=repo,
                    title=title,
                    body=body,
                    labels=labels,
                    assignees=assignees,
                    approval=resolved_approval,
                    allowed_repos=allowed_repos,
                    require_approval=require_approval,
                    cwd=cwd,
                )
            else:
                res = dict(result)
            if db is not None:
                db.add(
                    AuditLog(
                        actor=actor,
                        action="git.issue_create" if res.get("ok") else "git.issue_blocked",
                        resource_type="github_issue",
                        resource_id=res.get("repo"),
                        details={"repo": res.get("repo"), "allowed": res.get("allowed"), "ok": res.get("ok")},
                        severity="info" if res.get("ok") else "warning",
                    )
                )
                await db.commit()
            return res

        return _AwaitableResult(result, _with_db if db is not None else None)

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


git_workflow_manager = GitWorkflowManager()
