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
    verify_approval,
)
from app.models import ApprovalRequest, AuditLog, GitWorkflowRecord

PROTECTED_BRANCHES = {"main", "master", "production", "prod"}


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
