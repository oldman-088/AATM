"""Phase 4 API: pull-request review / merge view and project completion state."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.db.session import get_db
from app.git_workflows.manager import git_workflow_manager
from app.models import ApprovalRequest, AuditLog, Project, Task, User
from app.models.project import project_base_branch
from app.orchestration.review import review_service

router = APIRouter(tags=["reviews"], dependencies=[Depends(get_current_user)])


class MergeRequestBody(BaseModel):
    approval_id: str | None = None


def _serialize_pr_task(task: Task, reviews: list[Task], approvals: list[ApprovalRequest]) -> dict:
    return {
        "task_id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "status": task.status,
        "review_status": task.review_status,
        "review_round": task.review_round,
        "assigned_agent_id": task.assigned_agent_id,
        "branch_name": task.branch_name,
        "commit_sha": task.commit_sha,
        "pr_number": task.pr_number,
        "pr_url": task.pr_url,
        "pr_status": task.pr_status,
        "reviews": [
            {
                "task_id": r.id,
                "task_type": r.task_type,
                "status": r.status,
                "round": r.review_round,
                "assigned_agent_id": r.assigned_agent_id,
                "summary": r.result_summary,
            }
            for r in reviews
        ],
        "merge_approvals": [
            {"id": a.id, "status": a.status, "title": a.title, "escalated": bool((a.payload or {}).get("escalated"))}
            for a in approvals
        ],
    }


@router.get("/reviews/pull-requests")
async def list_pull_request_tasks(project_id: str | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(Task).where(Task.task_type == "implementation", Task.pr_number.is_not(None))
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    tasks = list((await db.execute(stmt.order_by(Task.updated_at.desc()))).scalars().all())
    ids = [t.id for t in tasks]
    helpers = list((await db.execute(select(Task).where(Task.parent_task_id.in_(ids)))).scalars().all()) if ids else []
    approvals = list(
        (await db.execute(select(ApprovalRequest).where(ApprovalRequest.action_type == "merge"))).scalars().all()
    )
    return [
        _serialize_pr_task(
            t,
            sorted([h for h in helpers if h.parent_task_id == t.id], key=lambda h: (h.review_round, h.created_at)),
            [a for a in approvals if (a.payload or {}).get("task_id") == t.id],
        )
        for t in tasks
    ]


@router.get("/reviews/tasks/{task_id}/diff")
async def get_pull_request_diff(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not task.worktree_path:
        return {"available": False, "code": "not_available", "detail": "Worktree was cleaned up (PR merged or closed)."}
    diff = await git_workflow_manager.branch_diff(task.worktree_path, project_base_branch(project))
    return {"available": bool(diff.get("ok")), **diff}


@router.get("/reviews/tasks/{task_id}/pr-state")
async def get_pull_request_state(task_id: str, db: AsyncSession = Depends(get_db)):
    """P1 PR card: the PR's state, conflicts and CI on its head commit, read from GitHub."""
    from app.github_client.client import github_client

    task = await db.get(Task, task_id)
    if task is None or not task.pr_number:
        raise HTTPException(status_code=404, detail="Task with a pull request not found")
    project = await db.get(Project, task.project_id)
    repo = git_workflow_manager.repo_slug_from_url(project.repo_url if project else None)
    if not repo:
        return {"available": False, "error": "The project has no GitHub repository URL."}
    return await github_client.pull_request_state(repo, task.pr_number)


@router.post("/reviews/tasks/{task_id}/resolve-conflict", dependencies=[Depends(require_role("admin", "operator"))])
async def resolve_conflict(task_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """P1 PR card: send the PR back to its worker to merge the base and resolve the conflict (once per head commit)."""
    from app.github_client.client import github_client
    from app.orchestration.merge_queue import merge_queue

    task = await db.get(Task, task_id)
    if task is None or not task.pr_number:
        raise HTTPException(status_code=404, detail="Task with a pull request not found")
    project = await db.get(Project, task.project_id)
    repo = git_workflow_manager.repo_slug_from_url(project.repo_url if project else None)
    if not repo:
        raise HTTPException(status_code=409, detail="The project has no GitHub repository URL.")
    state = await github_client.pull_request_state(repo, task.pr_number)
    if not state.get("available"):
        raise HTTPException(status_code=502, detail="GitHub did not answer; try again.")
    if state.get("state") != "OPEN" or not state.get("conflicting"):
        raise HTTPException(status_code=409, detail="The pull request has no merge conflict.")
    created = await merge_queue.conflict_follow_up(db, task, project_base_branch(project), state.get("head_sha") or "",
                                                   actor=user.email)
    return {"task_id": task.id, "pr_number": task.pr_number, "follow_up_created": created}


@router.post("/reviews/tasks/{task_id}/request-merge", dependencies=[Depends(require_role("admin", "operator"))])
async def request_merge_approval(task_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Open (or return) the merge ApprovalRequest for a PR; the merge itself still needs a human decision."""
    task = await db.get(Task, task_id)
    if task is None or not task.pr_number:
        raise HTTPException(status_code=404, detail="Task with an open pull request not found")
    if task.status in {"merged", "done"}:
        raise HTTPException(status_code=409, detail="Pull request is already merged")
    project = await db.get(Project, task.project_id)
    repo_slug = git_workflow_manager.repo_slug_from_url(project.repo_url if project else None)
    approval = await review_service._request_merge_approval(db, task, project, repo_slug, escalated=False)
    db.add(
        AuditLog(
            actor=user.email,
            action="review.merge_requested",
            resource_type="task",
            resource_id=task.id,
            details={"approval_id": approval.id, "pr_number": task.pr_number},
        )
    )
    await db.commit()
    return {"approval_id": approval.id, "status": approval.status}


@router.post("/reviews/tasks/{task_id}/merge", dependencies=[Depends(require_role("admin", "operator"))])
async def merge_pull_request(
    task_id: str,
    body: MergeRequestBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute a merge that a human already approved in the Approval Center."""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    approval = await db.get(ApprovalRequest, body.approval_id) if body.approval_id else None
    result = await review_service.merge_task(db, task, approval, actor=user.email)
    if not result.get("ok"):
        status = 403 if not result.get("allowed", True) else (501 if result.get("code") == "not_configured" else 502)
        raise HTTPException(status_code=status, detail={"code": result.get("code", "MERGE_FAILED"), "message": result.get("error")})
    return result


@router.get("/projects/{project_id}/completion")
async def get_project_completion(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project_id": project.id,
        "status": project.status,
        "completed": project.status == "completed",
        "completed_at": project.completed_at,
        "summary": project.completion_summary,
    }
