from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.events import event_bus
from app.db.base import utcnow
from app.db.session import get_db
from app.models import Agent, AuditLog, ChatMessage, Task, User
from app.orchestration.scheduler import task_scheduler
from app.schemas.common import AgentUpdate, ChatSendRequest, TaskCreate, TaskUpdate

router = APIRouter(tags=["agents-tasks-orchestration"])


def _serialize_agent(a: Agent) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "role": a.role,
        "role_template": a.role_template,
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
        "estimated_minutes": t.estimated_minutes,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


@router.get("/agents")
async def list_agents(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Agent).order_by(Agent.id.asc()))
    return [_serialize_agent(a) for a in res.scalars().all()]


@router.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    for field in ("status", "provider_id", "model_name", "is_enabled", "permissions"):
        val = getattr(body, field)
        if val is not None:
            setattr(agent, field, val)
    agent.last_heartbeat = utcnow()
    db.add(AuditLog(actor=user.email, action="agent.update", resource_type="agent", resource_id=agent.id, details=body.model_dump(exclude_none=True)))
    await db.commit()
    await db.refresh(agent)
    await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status})
    return _serialize_agent(agent)


@router.post("/agents/{agent_id}/restart")
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


@router.post("/tasks", status_code=201)
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


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: str,
    body: TaskUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if body.status == "completed":
        updated = await task_scheduler.complete_task(db, task, body.result_summary or "Completed")
        return _serialize_task(updated)
    if body.status == "failed":
        updated = await task_scheduler.fail_and_retry_or_escalate(db, task, body.error_message or "Execution failed")
        return _serialize_task(updated)

    for field in ("status", "priority", "assigned_agent_id", "progress_percent", "result_summary", "error_message"):
        val = getattr(body, field)
        if val is not None:
            setattr(task, field, val)
    if body.status == "cancelled":
        task.lock_owner = None
    await db.commit()
    await db.refresh(task)
    await event_bus.emit("task.updated", {"task_id": task.id, "status": task.status})
    return _serialize_task(task)


@router.post("/orchestration/cycle")
async def trigger_orchestration(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await task_scheduler.run_orchestration_cycle(db)


@router.get("/chat")
async def list_chat(project_id: str | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(ChatMessage)
    if project_id:
        stmt = stmt.where((ChatMessage.project_id == project_id) | (ChatMessage.project_id.is_(None)))
    res = await db.execute(stmt.order_by(ChatMessage.created_at.asc()))
    return [
        {
            "id": m.id,
            "project_id": m.project_id,
            "sender_type": m.sender_type,
            "sender_id": m.sender_id,
            "recipient_id": m.recipient_id,
            "content": m.content,
            "metadata": m.metadata_json,
            "created_at": m.created_at,
        }
        for m in res.scalars().all()
    ]


@router.post("/chat", status_code=201)
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
    reply = await task_scheduler.handle_manager_directive(db, body.project_id, body.content)
    return {
        "user_message": {
            "id": user_msg.id,
            "sender_type": user_msg.sender_type,
            "sender_id": user_msg.sender_id,
            "content": user_msg.content,
            "created_at": user_msg.created_at,
        },
        "manager_reply": {
            "id": reply.id,
            "sender_type": reply.sender_type,
            "sender_id": reply.sender_id,
            "content": reply.content,
            "metadata": reply.metadata_json,
            "created_at": reply.created_at,
        },
    }
