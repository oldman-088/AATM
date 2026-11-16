"""Agent messages, employee workspaces and SDLC workflow runs."""


from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.db.session import get_db
from app.models import (
    AgentMessage,
    User,
    WorkspaceArtifact,
)
from app.runtime.executor import WorkspaceSandboxError, agent_runtime
from app.schemas.common import (
    AgentMessageSend,
    SDLCRequest,
    WorkspaceExecRequest,
    WorkspaceWriteRequest,
)

router = APIRouter(tags=["enterprise-platform"], dependencies=[Depends(get_current_user)])


@router.get("/messages")
async def list_agent_messages(limit: int = 50, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AgentMessage).order_by(AgentMessage.created_at.desc()).limit(limit))).scalars().all()
    return list(rows)


@router.post("/messages", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def send_agent_message(
    body: AgentMessageSend,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await agent_runtime.send_and_execute_task_message(
            db=db,
            sender_id=body.sender_id,
            recipient_id=body.recipient_id,
            instructions=body.instructions,
            project_id=body.project_id,
            task_id=body.task_id,
            priority=body.priority,
            expected_deliverables=body.expected_deliverables,
            auto_execute=body.auto_execute,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/workspaces/{agent_id}/files")
async def list_workspace_files(agent_id: str, db: AsyncSession = Depends(get_db)):
    files = agent_runtime.list_workspace_files(agent_id)
    arts = (
        await db.execute(
            select(WorkspaceArtifact).where(WorkspaceArtifact.agent_id == agent_id).order_by(WorkspaceArtifact.created_at.desc()).limit(25)
        )
    ).scalars().all()
    return {"agent_id": agent_id, "files": files, "artifacts": list(arts)}


@router.post("/workspaces/{agent_id}/files", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def write_workspace_file(
    agent_id: str,
    body: WorkspaceWriteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await agent_runtime.write_workspace_file(
            db, agent_id, body.relative_path, body.content, project_id=body.project_id, task_id=body.task_id
        )
    except WorkspaceSandboxError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/workspaces/{agent_id}/exec", dependencies=[Depends(require_role("admin", "operator"))])
async def exec_workspace_command(
    agent_id: str,
    body: WorkspaceExecRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await agent_runtime.run_workspace_command(
            db, agent_id, body.command, project_id=body.project_id, task_id=body.task_id
        )
    except WorkspaceSandboxError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/sdlc/execute", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def run_sdlc_workflow(
    body: SDLCRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await agent_runtime.execute_sdlc_from_idea(db, idea=body.idea, project_name=body.project_name, actor=user.email)
