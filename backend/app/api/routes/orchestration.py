from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.events import event_bus
from app.db.backup import backup_before
from app.db.base import utcnow
from app.db.session import get_db
from app.git_workflows.manager import git_workflow_manager
from app.models import Agent, AuditLog, ChatMessage, GitWorkflowRecord, Project, Task, User
from app.models.project import project_base_branch
from app.orchestration.scheduler import task_scheduler
from app.orchestration.self_heal import release_worker_for_task
from app.schemas.common import AgentUpdate, ChatSendRequest, TaskCreate, TaskUpdate

router = APIRouter(tags=["agents-tasks-orchestration"], dependencies=[Depends(get_current_user)])

VALID_TASK_STATUSES = {
    "pending",
    "queued",
    "assigned",
    "in_progress",
    "pr_open",
    "in_review",
    "changes_requested",
    "approved",
    "merged",
    "done",
    "completed",
    "failed",
    "blocked",
    "cancelled",
}


def _serialize_agent(a: Agent) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "role": a.role,
        "role_template": a.role_template,
        "reports_to": a.reports_to,
        "specialty": a.specialty,
        "skills": a.skills,
        "divisions": a.divisions,
        "permissions": a.permissions,
        "status": a.status,
        "provider_id": a.provider_id,
        "provider_account_id": a.provider_account_id,
        "provider_session_id": a.provider_session_id,
        "model_name": a.model_name,
        "browser_profile_id": a.browser_profile_id,
        "workspace_path": a.workspace_path,
        "device_id": a.device_id,
        "autonomy_mode": a.autonomy_mode,
        "is_emergency_stopped": a.is_emergency_stopped,
        "git_branch": a.git_branch,
        "current_task_id": a.current_task_id,
        "cpu_percent": a.cpu_percent,
        "memory_mb": a.memory_mb,
        "tasks_completed": a.tasks_completed,
        "tasks_failed": a.tasks_failed,
        "performance_score": a.performance_score,
        "is_enabled": a.is_enabled,
        "last_heartbeat": a.last_heartbeat,
    }


def _serialize_task(t: Task) -> dict:
    return {
        "id": t.id,
        "project_id": t.project_id,
        "division_code": t.division_code,
        "title": t.title,
        "description": t.description,
        "priority": t.priority,
        "execution_mode": t.execution_mode,
        "status": t.status,
        "assigned_agent_id": t.assigned_agent_id,
        "collaborator_ids": t.collaborator_ids,
        "device_id": t.device_id,
        "dependencies": t.dependencies,
        "expected_deliverables": t.expected_deliverables,
        "actual_deliverables": t.actual_deliverables,
        "review_status": t.review_status,
        "idempotency_key": t.idempotency_key,
        "lock_owner": t.lock_owner,
        "retry_count": t.retry_count,
        "max_retries": t.max_retries,
        "progress_percent": t.progress_percent,
        "checkpoints": t.checkpoints,
        "result_summary": t.result_summary,
        "error_message": t.error_message,
        "branch_name": t.branch_name,
        "worktree_path": t.worktree_path,
        "commit_sha": t.commit_sha,
        "pr_number": t.pr_number,
        "pr_url": t.pr_url,
        "pr_status": t.pr_status,
        "estimated_minutes": t.estimated_minutes,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


@router.get("/agents")
async def list_agents(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Agent).order_by(Agent.id.asc()))
    return [_serialize_agent(a) for a in res.scalars().all()]


@router.patch("/agents/{agent_id}", dependencies=[Depends(require_role("admin", "operator"))])
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if body.provider_id is not None:
        from app.models import AIProvider

        provider = await db.get(AIProvider, body.provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail=f"Provider '{body.provider_id}' not found")
        agent.provider_id = provider.id
        if body.model_name is None and not agent.model_name:
            agent.model_name = provider.default_model
    for field in ("status", "model_name", "is_enabled", "permissions"):
        val = getattr(body, field)
        if val is not None:
            setattr(agent, field, val)
    if "reports_to" in body.model_fields_set:
        agent.reports_to = body.reports_to
    agent.last_heartbeat = utcnow()
    audit_details = body.model_dump(exclude_none=True)
    if "reports_to" in body.model_fields_set:
        audit_details["reports_to"] = body.reports_to
    db.add(AuditLog(actor=user.email, action="agent.update", resource_type="agent", resource_id=agent.id, details=audit_details))
    await db.commit()
    await db.refresh(agent)
    await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status})
    return _serialize_agent(agent)


@router.post("/agents/{agent_id}/restart", dependencies=[Depends(require_role("admin", "operator"))])
async def restart_agent(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.status = "idle"
    agent.current_task_id = None
    agent.is_enabled = True
    agent.last_heartbeat = utcnow()
    db.add(AuditLog(actor=user.email, action="agent.restart", resource_type="agent", resource_id=agent.id, details={}))
    await db.commit()
    await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": "idle"})
    return _serialize_agent(agent)


@router.get("/tasks")
async def list_tasks(
    project_id: str | None = None,
    status: str | None = None,
    division_code: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Task)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if status:
        stmt = stmt.where(Task.status == status)
    if division_code:
        stmt = stmt.where(Task.division_code == division_code)
    res = await db.execute(stmt.order_by(Task.created_at.desc()))
    return [_serialize_task(t) for t in res.scalars().all()]


@router.post("/tasks", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def create_task(
    body: TaskCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.idempotency_key:
        dup = await db.execute(select(Task).where(Task.idempotency_key == body.idempotency_key))
        existing = dup.scalar_one_or_none()
        if existing:
            return _serialize_task(existing)

    try:
        await task_scheduler.validate_dependencies(db, None, body.dependencies)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    task = Task(
        project_id=body.project_id,
        division_code=body.division_code,
        title=body.title,
        description=body.description,
        priority=body.priority,
        execution_mode=body.execution_mode,
        status="queued",
        assigned_agent_id=body.assigned_agent_id,
        collaborator_ids=body.collaborator_ids,
        dependencies=body.dependencies,
        expected_deliverables=body.expected_deliverables,
        idempotency_key=body.idempotency_key,
        estimated_minutes=body.estimated_minutes,
    )
    db.add(task)
    db.add(AuditLog(actor=user.email, action="task.create", resource_type="task", resource_id=task.id, details={"title": task.title}))
    await db.commit()
    await db.refresh(task)
    await event_bus.emit("task.updated", {"task_id": task.id, "status": task.status})
    return _serialize_task(task)


@router.patch("/tasks/{task_id}", dependencies=[Depends(require_role("admin", "operator"))])
async def update_task(
    task_id: str,
    body: TaskUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if body.status is not None and body.status not in VALID_TASK_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unsupported task status '{body.status}'")

    if body.status in {"completed", "done"}:
        if task.worktree_path:
            project = await db.get(Project, task.project_id)
            if project:
                await git_workflow_manager.cleanup_task_worktree(
                    repo_path=project.repo_path,
                    worktree_path=task.worktree_path,
                    branch_name=task.branch_name,
                    db=db,
                    task_id=task.id,
                    actor=user.email,
                )
            task.worktree_path = None
        updated = await task_scheduler.complete_task(
            db,
            task,
            body.result_summary or "Completed",
            target_status=body.status,
        )
        return _serialize_task(updated)
    if body.status == "failed":
        updated = await task_scheduler.fail_and_retry_or_escalate(db, task, body.error_message or "Execution failed")
        return _serialize_task(updated)

    for field in ("status", "priority", "assigned_agent_id", "progress_percent", "result_summary", "error_message", "review_status"):
        val = getattr(body, field)
        if val is not None:
            setattr(task, field, val)
    if body.status in {"cancelled", "merged"}:
        task.lock_owner = None
        task.locked_at = None
        await release_worker_for_task(db, task)
        if body.status == "merged":
            task.pr_status = "merged"
            task.progress_percent = 100
            task.completed_at = utcnow()
        if task.worktree_path:
            project = await db.get(Project, task.project_id)
            if project:
                await git_workflow_manager.cleanup_task_worktree(
                    repo_path=project.repo_path,
                    worktree_path=task.worktree_path,
                    branch_name=task.branch_name,
                    delete_remote_branch=(body.status == "merged"),
                    db=db,
                    task_id=task.id,
                    actor=user.email,
                )
            task.worktree_path = None
        if body.status == "merged":
            task.status = "done"
    await db.commit()
    await db.refresh(task)
    await event_bus.emit("task.updated", {"task_id": task.id, "status": task.status})
    return _serialize_task(task)


@router.post("/tasks/{task_id}/worktree", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def provision_task_worktree(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    res = await git_workflow_manager.create_task_worktree(
        task.id,
        task.title,
        repo_path=project.repo_path,
        base_branch=project_base_branch(project),
        db=db,
        actor=user.email,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Failed to create task worktree")
    task.branch_name = str(res["branch_name"])
    task.worktree_path = str(res["worktree_path"])
    wf = (await db.execute(select(GitWorkflowRecord).where(GitWorkflowRecord.task_id == task.id))).scalar_one_or_none()
    if wf is None:
        db.add(
            GitWorkflowRecord(
                project_id=project.id,
                task_id=task.id,
                branch_name=task.branch_name,
                worktree_path=task.worktree_path,
                owner_agent_id=task.assigned_agent_id,
                base_branch=str(res.get("base_branch") or project_base_branch(project)),
                pr_status="pending",
            )
        )
    await db.commit()
    await db.refresh(task)
    return {"task": _serialize_task(task), "worktree": res}


@router.post("/tasks/{task_id}/cleanup", dependencies=[Depends(require_role("admin", "operator"))])
async def cleanup_task_worktree_endpoint(
    task_id: str,
    delete_remote_branch: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    res = await git_workflow_manager.cleanup_task_worktree(
        repo_path=project.repo_path,
        worktree_path=task.worktree_path,
        branch_name=task.branch_name,
        delete_remote_branch=delete_remote_branch,
        db=db,
        task_id=task.id,
        actor=user.email,
    )
    task.worktree_path = None
    await db.commit()
    await db.refresh(task)
    return {"task": _serialize_task(task), "cleanup": res}


@router.post("/orchestration/cycle", dependencies=[Depends(require_role("admin", "operator"))])
async def trigger_orchestration(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await task_scheduler.run_orchestration_cycle(db)


def _chat_message_dict(m: ChatMessage) -> dict:
    return {
        "id": m.id,
        "project_id": m.project_id,
        "sender_type": m.sender_type,
        "sender_id": m.sender_id,
        "recipient_id": m.recipient_id,
        "content": m.content,
        "metadata": m.metadata_json,
        "created_at": m.created_at,
    }


CHAT_PAGE_DEFAULT = 200
CHAT_PAGE_MAX = 500


@router.get("/chat")
async def list_chat(
    project_id: str | None = None,
    limit: int = Query(default=CHAT_PAGE_DEFAULT, ge=1, le=CHAT_PAGE_MAX),
    before: str | None = Query(default=None, max_length=36, description="message id: return the page of messages older than it"),
    db: AsyncSession = Depends(get_db),
):
    """The newest ``limit`` messages (oldest first). Pass ``before=<id>`` to page back. Archived messages are hidden."""
    stmt = select(ChatMessage).where(ChatMessage.archived_at.is_(None))
    if project_id:
        stmt = stmt.where((ChatMessage.project_id == project_id) | (ChatMessage.project_id.is_(None)))
    if before:
        anchor = await db.get(ChatMessage, before)
        if anchor is None:
            raise HTTPException(status_code=404, detail="Chat message not found")
        stmt = stmt.where(
            (ChatMessage.created_at < anchor.created_at)
            | ((ChatMessage.created_at == anchor.created_at) & (ChatMessage.id < anchor.id))
        )
    res = await db.execute(stmt.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(limit))
    rows = list(res.scalars().all())
    rows.reverse()
    return [_chat_message_dict(m) for m in rows]


@router.delete("/chat", dependencies=[Depends(require_role("admin"))])
async def clear_chat(
    project_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clear the chat: archive its messages (hidden from GET /chat). Nothing is deleted and the audit log is untouched.

    A database backup is taken first; its path is returned and recorded in the audit entry.
    """
    stmt = select(ChatMessage).where(ChatMessage.archived_at.is_(None))
    if project_id:
        stmt = stmt.where(ChatMessage.project_id == project_id)
    rows = list((await db.execute(stmt)).scalars().all())
    backup_path = await backup_before(reason="chat clear")
    now = utcnow()
    for message in rows:
        message.archived_at = now
    db.add(
        AuditLog(
            actor=user.email,
            action="chat.clear",
            resource_type="chat",
            resource_id=project_id,
            details={"archived": len(rows), "project_id": project_id, "backup_path": backup_path},
        )
    )
    await db.commit()
    return {"archived": len(rows), "project_id": project_id, "backup_path": backup_path}


@router.post("/chat", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def send_chat(
    body: ChatSendRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_msg = ChatMessage(
        project_id=body.project_id,
        sender_type="user",
        sender_id=user.email,
        recipient_id=body.recipient_id,
        content=body.content,
    )
    db.add(user_msg)
    await db.commit()
    await db.refresh(user_msg)
    user_payload = {
        "id": user_msg.id,
        "sender_type": user_msg.sender_type,
        "sender_id": user_msg.sender_id,
        "content": user_msg.content,
        "created_at": user_msg.created_at,
    }
    reply = await task_scheduler.handle_manager_directive(
        db, body.project_id, body.content, user_message_id=user_msg.id, intent=body.intent
    )
    await db.refresh(reply)
    return {
        "user_message": user_payload,
        "manager_reply": {
            "id": reply.id,
            "sender_type": reply.sender_type,
            "sender_id": reply.sender_id,
            "content": reply.content,
            "metadata": reply.metadata_json,
            "created_at": reply.created_at,
        },
    }
