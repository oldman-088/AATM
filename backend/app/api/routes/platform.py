import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.browser.engines import BROWSER_ENGINE_REGISTRY, validate_userscript_policy
from app.core.config import get_settings
from app.core.events import event_bus
from app.core.security import mask_credential_ref, sanitize_untrusted_content
from app.db.base import utcnow
from app.db.session import get_db
from app.employees.catalog import (
    ROLE_TEMPLATES,
    analyze_team_skill_gaps,
    create_dynamic_employee,
    provision_missing_specialists,
)
from app.git_workflows.manager import git_workflow_manager
from app.github_client.client import github_client, sanitize_cli_output
from app.models import (
    AgentMessage,
    ApprovalRequest,
    AuditLog,
    ExecutionDevice,
    Notification,
    ProviderAccount,
    ProviderSession,
    ProviderUsageLog,
    Task,
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
    EmployeeSkillGapRequest,
    GitHubBranchCreateRequest,
    GitHubIssueCreateRequest,
    GitHubPRCommentRequest,
    GitHubPRCreateRequest,
    GitWorktreeCreateRequest,
    ProviderAccountCreate,
    SanitizeRequest,
    SDLCRequest,
    UserscriptCreate,
    WorkspaceExecRequest,
    WorkspaceWriteRequest,
)

router = APIRouter(tags=["enterprise-platform"])
sanitize_untrusted_text = sanitize_untrusted_content

_TRACE_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*")
_TRACE_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])['\"]?(api[_-]?key|access[_-]?token|refresh[_-]?token|device[_-]?token|token|secret|password|credential|authorization|cookie)['\"]?(\s*[:=]\s*)['\"]?([^\s,;\"'}]+)['\"]?"
)


def _redact_trace_text(value: str | None) -> str | None:
    """Mask credential-like key/value text before identifiers enter a trace response."""
    if value is None:
        return None
    value = _TRACE_BEARER_RE.sub(r"\1[REDACTED]", value)
    value = _TRACE_SECRET_RE.sub(r"\1\2[REDACTED]", value)
    return sanitize_cli_output(value)


def _trace_utc(value: datetime) -> datetime:
    """Normalize DB timestamps for stable cross-source ordering and ISO serialization."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _audit_trace_links(
    audit: AuditLog,
    task_by_id: dict[str, Task],
) -> tuple[str | None, str | None]:
    details = audit.details if isinstance(audit.details, dict) else {}
    agent_id = details.get("agent_id") or details.get("assigned_agent_id")
    project_id = details.get("project_id")
    if audit.resource_type == "agent":
        agent_id = agent_id or audit.resource_id
    elif audit.resource_type == "project":
        project_id = project_id or audit.resource_id
    elif audit.resource_type == "task" and audit.resource_id in task_by_id:
        task = task_by_id[audit.resource_id]
        agent_id = agent_id or task.assigned_agent_id
        project_id = project_id or task.project_id
    return (str(agent_id) if agent_id is not None else None, str(project_id) if project_id is not None else None)


def _raise_git_worktree_error(result: dict[str, Any]) -> None:
    detail = str(result.get("error") or result.get("reason") or "Git worktree operation failed")
    approval_failure = (
        bool(result.get("approval_id"))
        or result.get("is_protected_target") is True
        or "approval" in detail.casefold()
    )
    raise HTTPException(status_code=403 if approval_failure else 400, detail=detail)


def _sanitize_github_webhook_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    sanitized = sanitize_untrusted_text(text[:5_000], source="github_webhook")
    return sanitize_cli_output(str(sanitized.get("quarantined_text") or ""))[:6_000]


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
    if emp.role != "manager":
        emp.reports_to = body.reports_to or "manager-0"
    await db.commit()
    await db.refresh(emp)
    return {
        "id": emp.id,
        "name": emp.name,
        "role": emp.role,
        "role_template": emp.role_template,
        "reports_to": emp.reports_to,
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


@router.post("/employees/skill-gaps")
async def analyze_employee_skill_gaps(
    body: EmployeeSkillGapRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await analyze_team_skill_gaps(db, body.required_skills, divisions=body.divisions)


@router.post("/employees/provision-specialists", status_code=201)
async def provision_employee_specialists(
    body: EmployeeSkillGapRequest,
    user: User = Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    return await provision_missing_specialists(
        db,
        body.required_skills,
        divisions=body.divisions,
        actor=user.email,
    )


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


USERSCRIPT_STORAGE_ONLY_NOTICE = (
    "AATM stores and audits userscripts but does not inject or execute them. "
    "Regex policy checks are bypassable (for example, window['ev'+'al']) and are not a security boundary."
)


def _serialize_userscript(script: Userscript) -> dict[str, Any]:
    """Expose stored policy metadata without implying script execution or reporting fake run counters."""
    return {
        "id": script.id,
        "name": script.name,
        "namespace": script.namespace,
        "version": script.version,
        "description": script.description,
        "match_patterns": script.match_patterns,
        "allowed_roles": script.allowed_roles,
        "allowed_agent_ids": script.allowed_agent_ids,
        "code": script.code,
        "is_enabled": script.is_enabled,
        "audit_status": script.audit_status,
        "created_at": script.created_at,
        "storage_only": True,
        "execution_supported": False,
        "policy_checks_security_boundary": False,
        "policy_notice": USERSCRIPT_STORAGE_ONLY_NOTICE,
    }


@router.get("/userscripts")
async def list_userscripts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Userscript).order_by(Userscript.created_at.desc()))).scalars().all()
    return [_serialize_userscript(row) for row in rows]


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
    return _serialize_userscript(us)


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
    return _serialize_userscript(us)


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


@router.post("/git/worktrees", status_code=201)
async def create_agent_git_worktree(
    body: GitWorktreeCreateRequest,
    user: User = Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    approval = None
    if body.approval_id is not None:
        approval = await db.get(ApprovalRequest, body.approval_id)
        if approval is None or approval.action_type != "branch":
            raise HTTPException(status_code=403, detail="A valid branch approval is required")

    result = await git_workflow_manager.create_agent_worktree(
        agent_id=body.agent_id,
        branch_name=body.branch_name,
        base_ref=body.base_ref,
        db=db,
        approval=approval,
        actor=user.email,
    )
    if not result.get("allowed") or not result.get("ok"):
        _raise_git_worktree_error(result)
    return result


@router.delete("/git/worktrees/{agent_id}")
async def remove_agent_git_worktree(
    agent_id: str,
    user: User = Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    result = await git_workflow_manager.remove_agent_worktree(
        agent_id=agent_id,
        db=db,
        actor=user.email,
    )
    if not result.get("allowed") or not result.get("ok"):
        _raise_git_worktree_error(result)
    return result


@router.post("/github/branches", status_code=201)
async def create_github_branch(
    body: GitHubBranchCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    approval = await db.get(ApprovalRequest, body.approval_id)
    if approval is None or approval.action_type != "branch":
        raise HTTPException(status_code=403, detail="GitHub branch creation requires a branch approval")

    approved_payload = approval.payload if isinstance(approval.payload, dict) else {}
    if any(
        approved_payload.get(field) != requested
        for field, requested in (
            ("repo", body.repo),
            ("branch_name", body.branch_name),
            ("from_branch", body.from_branch),
        )
    ):
        raise HTTPException(status_code=403, detail="Approval does not match the requested branch write")

    result = await git_workflow_manager.create_branch(
        branch_name=body.branch_name,
        base_branch=body.from_branch,
        repo=body.repo,
        approval=approval,
        require_approval=True,
        db=db,
        approval_id=body.approval_id,
        actor=user.email,
    )
    if not result.get("allowed"):
        raise HTTPException(status_code=403, detail=result.get("error") or "GitHub branch blocked by approval/branch policy")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "GitHub branch creation failed")
    return result


@router.get("/traces")
async def get_execution_traces(
    agent_id: str | None = None,
    project_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a read-only timeline made only from persisted execution records.

    Free-form instructions, audit details, artifact summaries, provider errors, and
    message payloads are intentionally omitted because they may contain credentials.
    """
    scan_limit = min(limit * 5, 1000)
    audit_rows = list(
        (
            await db.execute(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(scan_limit)
            )
        ).scalars().all()
    )
    task_ids = [
        audit.resource_id
        for audit in audit_rows
        if audit.resource_type == "task" and audit.resource_id is not None
    ]
    task_by_id: dict[str, Task] = {}
    if task_ids:
        task_rows = await db.execute(select(Task).where(Task.id.in_(task_ids)))
        task_by_id = {task.id: task for task in task_rows.scalars().all()}

    traces: list[dict[str, Any]] = []
    for audit in audit_rows:
        linked_agent, linked_project = _audit_trace_links(audit, task_by_id)
        if agent_id is not None and linked_agent is None and audit.actor == agent_id:
            linked_agent = agent_id
        if agent_id is not None and linked_agent != agent_id:
            continue
        if project_id is not None and linked_project != project_id:
            continue
        traces.append(
            {
                "id": _redact_trace_text(f"audit:{audit.id}"),
                "source": "audit_log",
                "source_id": _redact_trace_text(str(audit.id)),
                "timestamp": _trace_utc(audit.created_at),
                "agent_id": _redact_trace_text(linked_agent),
                "project_id": _redact_trace_text(linked_project),
                "actor": _redact_trace_text(audit.actor),
                "action": _redact_trace_text(audit.action),
                "resource_type": _redact_trace_text(audit.resource_type),
                "resource_id": _redact_trace_text(audit.resource_id),
                "severity": _redact_trace_text(audit.severity),
            }
        )

    usage_stmt = (
        select(ProviderUsageLog, Task.project_id, Task.assigned_agent_id)
        .outerjoin(Task, ProviderUsageLog.task_id == Task.id)
        .order_by(ProviderUsageLog.created_at.desc())
        .limit(scan_limit)
    )
    if agent_id is not None:
        usage_stmt = usage_stmt.where(
            or_(ProviderUsageLog.agent_id == agent_id, Task.assigned_agent_id == agent_id)
        )
    if project_id is not None:
        usage_stmt = usage_stmt.where(Task.project_id == project_id)
    usage_rows = (await db.execute(usage_stmt)).all()
    for usage, linked_project, task_agent in usage_rows:
        traces.append(
            {
                "id": _redact_trace_text(f"provider_usage:{usage.id}"),
                "source": "provider_usage_log",
                "source_id": _redact_trace_text(str(usage.id)),
                "timestamp": _trace_utc(usage.created_at),
                "agent_id": _redact_trace_text(usage.agent_id or task_agent),
                "project_id": _redact_trace_text(linked_project),
                "provider_id": _redact_trace_text(usage.provider_id),
                "task_id": _redact_trace_text(usage.task_id),
                "model_name": _redact_trace_text(usage.model_name),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "estimated_cost_usd": usage.estimated_cost_usd,
                "latency_ms": usage.latency_ms,
                "success": usage.success,
            }
        )

    artifact_stmt = select(WorkspaceArtifact).order_by(WorkspaceArtifact.created_at.desc()).limit(scan_limit)
    if agent_id is not None:
        artifact_stmt = artifact_stmt.where(WorkspaceArtifact.agent_id == agent_id)
    if project_id is not None:
        artifact_stmt = artifact_stmt.where(WorkspaceArtifact.project_id == project_id)
    artifact_rows = (await db.execute(artifact_stmt)).scalars().all()
    for artifact in artifact_rows:
        traces.append(
            {
                "id": _redact_trace_text(f"workspace_artifact:{artifact.id}"),
                "source": "workspace_artifact",
                "source_id": _redact_trace_text(str(artifact.id)),
                "timestamp": _trace_utc(artifact.created_at),
                "agent_id": _redact_trace_text(artifact.agent_id),
                "project_id": _redact_trace_text(artifact.project_id),
                "task_id": _redact_trace_text(artifact.task_id),
                "action": _redact_trace_text(artifact.action),
                "relative_path": _redact_trace_text(artifact.relative_path),
                "exit_code": artifact.exit_code,
            }
        )

    message_stmt = select(AgentMessage).order_by(AgentMessage.created_at.desc()).limit(scan_limit)
    if agent_id is not None:
        message_stmt = message_stmt.where(
            or_(AgentMessage.sender_id == agent_id, AgentMessage.recipient_id == agent_id)
        )
    if project_id is not None:
        message_stmt = message_stmt.where(AgentMessage.project_id == project_id)
    message_rows = (await db.execute(message_stmt)).scalars().all()
    for message in message_rows:
        traces.append(
            {
                "id": _redact_trace_text(f"agent_message:{message.id}"),
                "source": "agent_message",
                "source_id": _redact_trace_text(str(message.id)),
                "timestamp": _trace_utc(message.created_at),
                "agent_id": _redact_trace_text(
                    agent_id if agent_id is not None else message.sender_id
                ),
                "project_id": _redact_trace_text(message.project_id),
                "task_id": _redact_trace_text(message.task_id),
                "sender_id": _redact_trace_text(message.sender_id),
                "recipient_id": _redact_trace_text(message.recipient_id),
                "channel_type": _redact_trace_text(message.channel_type),
                "priority": _redact_trace_text(message.priority),
                "execution_status": _redact_trace_text(message.execution_status),
                "delivery_status": _redact_trace_text(message.delivery_status),
            }
        )

    traces.sort(key=lambda trace: trace["timestamp"], reverse=True)
    traces = traces[:limit]
    for trace in traces:
        trace["timestamp"] = trace["timestamp"].isoformat()

    return {
        "traces": traces,
        "count": len(traces),
        "filters": {
            "agent_id": _redact_trace_text(agent_id),
            "project_id": _redact_trace_text(project_id),
        },
    }


@router.post("/github/prs/{pr_number}/comments", status_code=201)
async def comment_on_github_pull_request(
    pr_number: int,
    body: GitHubPRCommentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if pr_number < 1:
        raise HTTPException(status_code=400, detail="Pull request number must be positive")
    if not body.approval_id:
        raise HTTPException(status_code=403, detail="An approved pull request comment/review request is required")

    approval = await db.get(ApprovalRequest, body.approval_id)
    if approval is None or approval.status != "approved":
        raise HTTPException(status_code=403, detail="An approved pull request comment/review request is required")
    approved_payload = approval.payload if isinstance(approval.payload, dict) else {}
    approved_repo = approved_payload.get("repo")
    try:
        approved_pr_number = int(approved_payload.get("pr_number"))
    except (TypeError, ValueError):
        approved_pr_number = None
    if (
        not isinstance(approved_repo, str)
        or approved_repo.strip().casefold() != body.repo.strip().casefold()
        or approved_pr_number != pr_number
        or approved_payload.get("body") != body.body
        or str(approved_payload.get("event") or "").strip().upper() != body.event
    ):
        raise HTTPException(status_code=403, detail="Approval does not match the requested pull request comment/review")

    result = github_client.comment_on_pull_request(
        repo=body.repo,
        pr_number=pr_number,
        body=body.body,
        event=body.event,
        approval=approval,
    )
    if not result.get("allowed"):
        raise HTTPException(status_code=403, detail=result.get("error") or "Pull request comment/review blocked by policy")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Pull request comment/review failed")

    db.add(
        AuditLog(
            actor=user.email,
            action="github.pr_review",
            resource_type="github_pull_request",
            resource_id=str(pr_number),
            details={
                "repo": body.repo,
                "event": body.event,
                "approval_id": body.approval_id,
                "ok": True,
            },
        )
    )
    await db.commit()
    return result


@router.post("/github/webhooks", status_code=202)
async def receive_github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    signing_key = getattr(settings, "GITHUB_WEBHOOK_SECRET", "") or os.getenv("AATM_GITHUB_WEBHOOK_SECRET", "")
    if not isinstance(signing_key, str) or not signing_key.strip():
        raise HTTPException(status_code=503, detail="GitHub webhook verification is not configured")

    raw_body = await request.body()
    expected_signature = "sha256=" + hmac.new(signing_key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if (
        not x_hub_signature_256
        or re.fullmatch(r"sha256=[0-9a-fA-F]{64}", x_hub_signature_256) is None
        or not hmac.compare_digest(x_hub_signature_256, expected_signature)
    ):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="GitHub webhook payload must be valid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="GitHub webhook payload must be a JSON object")

    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    pull_request = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    subject = pull_request or issue
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    raw_number = subject.get("number")
    try:
        number = int(raw_number) if not isinstance(raw_number, bool) and raw_number is not None else None
    except (TypeError, ValueError):
        number = None

    delivery_fingerprint = hashlib.sha256((x_github_delivery or hashlib.sha256(raw_body).hexdigest()).encode()).hexdigest()
    safe_payload = {
        "delivery_id": _sanitize_github_webhook_text(x_github_delivery),
        "event": _sanitize_github_webhook_text(x_github_event or "unknown"),
        "action": _sanitize_github_webhook_text(payload.get("action")),
        "repository": _sanitize_github_webhook_text(repository.get("full_name") or repository.get("name")),
        "sender": _sanitize_github_webhook_text(sender.get("login")),
        "number": number,
        "title": _sanitize_github_webhook_text(subject.get("title")),
    }
    notification_title = f"GitHub webhook received: {safe_payload['event']}"[:200]
    notification_message = "\n".join(
        f"{key}: {value}" for key, value in safe_payload.items() if value not in (None, "")
    )[:4_000]
    db.add(
        AuditLog(
            actor="github-webhook",
            action="github.webhook",
            resource_type="github_webhook",
            resource_id=delivery_fingerprint[:64],
            details=safe_payload,
        )
    )
    db.add(
        Notification(
            category="github",
            severity="info",
            title=notification_title,
            message=notification_message,
        )
    )
    await db.commit()
    await event_bus.emit("github.webhook", safe_payload)
    return {"accepted": True, "delivery_id": safe_payload["delivery_id"], "event": safe_payload["event"]}


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
