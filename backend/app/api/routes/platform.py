import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.browser.engines import BROWSER_ENGINE_REGISTRY, validate_userscript_policy
from app.core.security import mask_credential_ref, sanitize_untrusted_content
from app.db.base import utcnow
from app.db.session import get_db
from app.employees.catalog import ROLE_TEMPLATES, create_dynamic_employee
from app.git_workflows.manager import git_workflow_manager
from app.github_client.client import github_client
from app.models import (
    AgentMessage,
    ExecutionDevice,
    ProviderAccount,
    ProviderSession,
    User,
    Userscript,
    WorkspaceArtifact,
)
from app.orchestration.replacement import replacement_service
from app.runtime.device_auth import issue_device_token
from app.runtime.executor import WorkspaceSandboxError, agent_runtime
from app.schemas.common import (
    AgentMessageSend,
    DeviceRegisterRequest,
    EmergencyStopRequest,
    EmployeeCreate,
    GitHubIssueCreateRequest,
    GitHubPRCreateRequest,
    ProviderAccountCreate,
    SanitizeRequest,
    SDLCRequest,
    UserscriptCreate,
    WorkspaceExecRequest,
    WorkspaceWriteRequest,
)

router = APIRouter(tags=["enterprise-platform"])


@router.get("/employees/templates")
async def list_role_templates():
    return ROLE_TEMPLATES


@router.post("/employees", status_code=201)
async def create_employee_endpoint(
    body: EmployeeCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    emp = await create_dynamic_employee(
        db=db,
        name=body.name,
        role_template_id=body.role_template_id,
        specialty=body.specialty,
        skills=body.skills,
        divisions=body.divisions,
        permissions=body.permissions,
        provider_id=body.provider_id,
        allocation_mode=body.allocation_mode,
        preferred_account_id=body.preferred_account_id,
        create_browser_profile=body.create_browser_profile,
        autonomy_mode=body.autonomy_mode,
        device_id=body.device_id,
        actor=user.email,
    )
    return {
        "id": emp.id,
        "name": emp.name,
        "role": emp.role,
        "role_template": emp.role_template,
        "specialty": emp.specialty,
        "skills": emp.skills,
        "divisions": emp.divisions,
        "permissions": emp.permissions,
        "provider_id": emp.provider_id,
        "provider_account_id": emp.provider_account_id,
        "provider_session_id": emp.provider_session_id,
        "browser_profile_id": emp.browser_profile_id,
        "workspace_path": emp.workspace_path,
        "autonomy_mode": emp.autonomy_mode,
        "status": emp.status,
    }


@router.post("/employees/{agent_id}/replace")
async def replace_employee_endpoint(
    agent_id: str,
    reason: str = "Unavailable or repeated failures",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await replacement_service.replace_employee(db, agent_id, reason=reason, actor=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/accounts")
async def list_provider_accounts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ProviderAccount).order_by(ProviderAccount.created_at.desc()))).scalars().all()
    return [
        {
            "id": a.id,
            "provider_id": a.provider_id,
            "label": a.label,
            "account_identifier": a.account_identifier,
            "allocation_mode": a.allocation_mode,
            "max_employees": a.max_employees,
            "credential_status": mask_credential_ref(a.credential_ref),
            "status": a.status,
            "created_at": a.created_at,
        }
        for a in rows
    ]


@router.post("/accounts", status_code=201)
async def create_provider_account(
    body: ProviderAccountCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    acc = ProviderAccount(
        id=f"acct-{uuid.uuid4().hex[:8]}",
        provider_id=body.provider_id,
        label=body.label,
        account_identifier=body.account_identifier,
        allocation_mode=body.allocation_mode,
        max_employees=body.max_employees,
        credential_ref=body.credential_ref,
        status="active",
        last_verified_at=utcnow(),
    )
    db.add(acc)
    await db.commit()
    await db.refresh(acc)
    return acc


@router.get("/sessions")
async def list_provider_sessions(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ProviderSession).order_by(ProviderSession.created_at.desc()))).scalars().all()
    return list(rows)


@router.post("/sessions/{session_id}/recover")
async def recover_provider_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    sess = await db.get(ProviderSession, session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Provider session not found")
    sess.status = "active"
    sess.health_score = 100.0
    sess.last_error = None
    await db.commit()
    await db.refresh(sess)
    return sess


@router.get("/browsers/engines")
async def list_browser_engines():
    return {k: v.capabilities() for k, v in BROWSER_ENGINE_REGISTRY.items()}


@router.get("/userscripts")
async def list_userscripts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Userscript).order_by(Userscript.created_at.desc()))).scalars().all()
    return list(rows)


@router.post("/userscripts", status_code=201)
async def create_userscript(
    body: UserscriptCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok, msg = validate_userscript_policy(
        body.code,
        body.match_patterns,
        allowed_roles=body.allowed_roles,
        allowed_agent_ids=body.allowed_agent_ids,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    us = Userscript(
        id=f"us-{uuid.uuid4().hex[:8]}",
        name=body.name,
        description=body.description,
        match_patterns=body.match_patterns,
        allowed_roles=body.allowed_roles,
        allowed_agent_ids=body.allowed_agent_ids,
        code=body.code,
        is_enabled=True,
        audit_status="approved",
    )
    db.add(us)
    await db.commit()
    await db.refresh(us)
    return us


@router.patch("/userscripts/{script_id}/toggle")
async def toggle_userscript(
    script_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    us = await db.get(Userscript, script_id)
    if not us:
        raise HTTPException(status_code=404, detail="Userscript not found")
    us.is_enabled = not us.is_enabled
    await db.commit()
    await db.refresh(us)
    return us


@router.get("/messages")
async def list_agent_messages(limit: int = 50, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AgentMessage).order_by(AgentMessage.created_at.desc()).limit(limit))).scalars().all()
    return list(rows)


@router.post("/messages", status_code=201)
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


@router.post("/workspaces/{agent_id}/files", status_code=201)
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


@router.post("/workspaces/{agent_id}/exec")
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


@router.post("/sdlc/execute", status_code=201)
async def run_sdlc_workflow(
    body: SDLCRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await agent_runtime.execute_sdlc_from_idea(db, idea=body.idea, project_name=body.project_name, actor=user.email)


@router.get("/devices")
async def list_devices(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ExecutionDevice).order_by(ExecutionDevice.registered_at.asc()))).scalars().all()
    return list(rows)


@router.post("/devices", status_code=201)
async def register_device(
    body: DeviceRegisterRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register an execution device (operator action) and mint its device token.

    The returned `device_token` is the credential the remote worker daemon uses for
    heartbeat / claim / report calls. It is shown once, here.
    """
    existing = await db.get(ExecutionDevice, body.id)
    if existing:
        existing.status = "online"
        existing.last_heartbeat = utcnow()
        await db.commit()
        await db.refresh(existing)
        return {
            "id": existing.id,
            "name": existing.name,
            "device_type": existing.device_type,
            "host_address": existing.host_address,
            "os_name": existing.os_name,
            "cpu_cores": existing.cpu_cores,
            "ram_gb": existing.ram_gb,
            "capabilities": existing.capabilities,
            "status": existing.status,
            "max_concurrent_tasks": existing.max_concurrent_tasks,
            "last_heartbeat": existing.last_heartbeat,
            "registered_at": existing.registered_at,
            "device_token": issue_device_token(existing.id, existing.token_version),
        }

    dev = ExecutionDevice(
        id=body.id,
        name=body.name,
        device_type=body.device_type,
        host_address=body.host_address,
        os_name=body.os_name,
        cpu_cores=body.cpu_cores,
        ram_gb=body.ram_gb,
        capabilities=body.capabilities,
        status="online",
        max_concurrent_tasks=body.max_concurrent_tasks,
    )
    db.add(dev)
    await db.commit()
    await db.refresh(dev)
    return {
        "id": dev.id,
        "name": dev.name,
        "device_type": dev.device_type,
        "host_address": dev.host_address,
        "os_name": dev.os_name,
        "cpu_cores": dev.cpu_cores,
        "ram_gb": dev.ram_gb,
        "capabilities": dev.capabilities,
        "status": dev.status,
        "max_concurrent_tasks": dev.max_concurrent_tasks,
        "last_heartbeat": dev.last_heartbeat,
        "registered_at": dev.registered_at,
        "device_token": issue_device_token(dev.id, dev.token_version),
    }


@router.post("/emergency-stop")
async def trigger_emergency_stop(
    body: EmergencyStopRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await replacement_service.emergency_stop(
        db, scope=body.scope, target_id=body.target_id, actor=user.email, reason=body.reason
    )


@router.post("/emergency-stop/clear")
async def clear_emergency_stop(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await replacement_service.clear_emergency_stop(db, actor=user.email)


@router.post("/security/sanitize")
async def sanitize_external_text(body: SanitizeRequest):
    return sanitize_untrusted_content(body.text, source=body.source)


@router.get("/github/repos")
async def list_github_repos():
    return github_client.list_repositories()


@router.get("/github/prs")
async def list_github_prs(repo: str | None = None):
    return github_client.list_pull_requests(repo=repo)


@router.get("/github/issues")
async def list_github_issues(repo: str | None = None):
    return github_client.list_issues(repo=repo)


@router.get("/github/summary")
async def github_change_summary():
    return {
        "auth": github_client.get_auth_status(),
        "changes": github_client.generate_change_summary(),
    }


@router.post("/github/prs", status_code=201)
async def create_github_pull_request(
    body: GitHubPRCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await git_workflow_manager.create_pull_request(
        repo=body.repo,
        title=body.title,
        head=body.head,
        base=body.base,
        body=body.body,
        draft=body.draft,
        db=db,
        approval_id=body.approval_id,
        actor=user.email,
    )
    if not res.get("allowed"):
        raise HTTPException(status_code=403, detail=res.get("error") or "GitHub PR blocked by approval/branch policy")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "GitHub PR creation failed")
    return res


@router.post("/github/issues", status_code=201)
async def create_github_issue(
    body: GitHubIssueCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await git_workflow_manager.create_issue(
        repo=body.repo,
        title=body.title,
        body=body.body,
        labels=body.labels,
        assignees=body.assignees,
        db=db,
        approval_id=body.approval_id,
        actor=user.email,
    )
    if not res.get("allowed"):
        raise HTTPException(status_code=403, detail=res.get("error") or "GitHub issue blocked by approval policy")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "GitHub issue creation failed")
    return res
