import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import ApprovalRequest, AuditLog, GitWorkflowRecord

PROTECTED_BRANCHES = {"main", "master", "production", "prod"}


class GitWorkflowManager:
    """Inspects real Git repository state, worktrees, branches, and enforces protected-branch approvals."""

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
            return proc.returncode, (proc.stdout or proc.stderr).strip()
        except Exception as exc:
            return 1, str(exc)

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
            "is_protected": branch in PROTECTED_BRANCHES,
            "dirty_files": [ln for ln in status_out.splitlines() if ln.strip()],
            "recent_commits": [ln for ln in log_out.splitlines() if ln.strip()],
            "worktrees": worktrees,
        }

    async def request_protected_merge(
        self,
        db: AsyncSession,
        record: GitWorkflowRecord,
        actor: str,
    ) -> dict[str, Any]:
        target = record.base_branch.lower()
        if target in PROTECTED_BRANCHES:
            approved_res = await db.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.action_type == "merge",
                    ApprovalRequest.status == "approved",
                    ApprovalRequest.project_id == record.project_id,
                )
            )
            if not approved_res.scalar_one_or_none():
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
