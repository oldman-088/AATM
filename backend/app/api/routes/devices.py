"""Multi-device execution API for remote AATM worker daemons.

The central server keeps the authoritative `ExecutionDevice` registry and the task
queue. A remote worker daemon registers itself, heartbeats, claims a task and reports
the outcome. Every endpoint except `GET /devices` is authenticated with a device-scoped
token (see `app.runtime.device_auth`).
"""

import json
import math
import re
from datetime import timedelta
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional, require_role
from app.core.config import get_settings
from app.core.events import event_bus
from app.core.security import sanitize_untrusted_content
from app.db.base import utcnow
from app.db.session import get_db
from app.models import (
    Agent,
    AuditLog,
    ExecutionDevice,
    Project,
    ProviderUsageLog,
    SystemSetting,
    Task,
    User,
    WorkspaceArtifact,
)
from app.orchestration.scheduler import PRIORITY_WEIGHT, task_scheduler
from app.providers.base import ProviderRequest
from app.providers.router import provider_router
from app.runtime.agent_protocol import REMOTE_ACTION_PROTOCOL, build_system_prompt, redact_secrets, safe_excerpt
from app.runtime.device_auth import issue_device_token, using_default_secret, verify_device_token
from app.runtime.sandbox import is_sensitive_workspace_path
from app.runtime.temporal import to_naive_utc
from app.schemas.common import (
    DeviceAgentModelRequest,
    DeviceTaskProgressRequest,
    DeviceTaskReportRequest,
    DeviceTokenRevokeResponse,
    RemoteArtifactReport,
)

router = APIRouter(tags=["multi-device-execution"])

# A device that has not been heard from within this window is considered disconnected
# and the tasks it owned are requeued.
DEVICE_OFFLINE_MINUTES = 5


def _bound_agent_id(device_id: str) -> str:
    """Deterministic employee id that represents a device on the server."""
    return f"dev-{device_id}"[:64]


async def require_device_token(device_id: str, token: str | None, db: AsyncSession) -> ExecutionDevice:
    """Load a device and require a token signed for its current revocation version."""
    device = await db.get(ExecutionDevice, device_id)
    if device is None or not verify_device_token(device_id, token, device.token_version):
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")
    return device


def _validate_remote_path(path: str, *, allow_workspace_root: bool = False) -> str:
    """Validate a device-reported POSIX relative path without trusting its host OS."""
    if allow_workspace_root and path == ".":
        return path
    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    if (
        not path
        or "\\" in path
        or "\x00" in path
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
        or is_sensitive_workspace_path(path)
    ):
        raise HTTPException(status_code=422, detail="Remote workspace paths must be safe, relative, and non-sensitive.")
    return posix_path.as_posix()


async def _require_claimed_task(
    db: AsyncSession,
    device_id: str,
    task_id: str,
    worker_id: str,
) -> tuple[Task, Agent]:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if (
        task.device_id != device_id
        or task.assigned_agent_id != _bound_agent_id(device_id)
        or task.status != "in_progress"
        or task.lock_owner != worker_id
    ):
        raise HTTPException(status_code=409, detail="Task is not currently claimed by this device worker.")
    agent = await db.get(Agent, _bound_agent_id(device_id))
    if agent is None or not agent.is_enabled or agent.is_emergency_stopped:
        raise HTTPException(status_code=409, detail="The device worker is disabled or emergency-stopped.")
    return task, agent


async def _persist_remote_artifact(
    db: AsyncSession,
    device_id: str,
    task: Task,
    artifact: RemoteArtifactReport,
) -> None:
    path = _validate_remote_path(artifact.relative_path, allow_workspace_root=True)
    normalized_summary = safe_excerpt(artifact.summary, 1_000)
    existing = await db.execute(
        select(WorkspaceArtifact.id).where(
            WorkspaceArtifact.agent_id == _bound_agent_id(device_id),
            WorkspaceArtifact.task_id == task.id,
            WorkspaceArtifact.relative_path == path,
            WorkspaceArtifact.action == artifact.action,
            WorkspaceArtifact.exit_code == artifact.exit_code,
            WorkspaceArtifact.summary == normalized_summary,
        ).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return
    db.add(
        WorkspaceArtifact(
            agent_id=_bound_agent_id(device_id),
            project_id=task.project_id,
            task_id=task.id,
            relative_path=path,
            action=artifact.action,
            summary=normalized_summary,
            exit_code=artifact.exit_code,
        )
    )


async def _task_budget(db: AsyncSession, task_id: str) -> tuple[float, float]:
    spent_result = await db.execute(
        select(func.coalesce(func.sum(ProviderUsageLog.estimated_cost_usd), 0.0)).where(
            ProviderUsageLog.task_id == task_id
        )
    )
    spent = float(spent_result.scalar_one() or 0.0)
    default_limit = float(get_settings().AGENT_TASK_BUDGET_USD)
    limit = default_limit if math.isfinite(default_limit) and default_limit >= 0 else 5.0
    setting = await db.get(SystemSetting, "budgets")
    if setting and isinstance(setting.value, dict):
        try:
            configured = setting.value.get("per_task_limit_usd")
            if configured is not None:
                candidate = float(configured)
                if math.isfinite(candidate) and candidate >= 0:
                    limit = candidate
        except (TypeError, ValueError):
            pass
    return spent, limit


async def _ensure_bound_agent(
    db: AsyncSession,
    device: ExecutionDevice,
    workspace_root: str | None = None,
    worker_id: str | None = None,
) -> Agent:
    """Create (or refresh) the server-side employee that represents a remote device."""
    agent_id = _bound_agent_id(device.id)
    agent = await db.get(Agent, agent_id)
    if agent is None:
        agent = Agent(
            id=agent_id,
            name=f"Remote Worker :: {device.name}",
            role="worker",
            role_template="devops_engineer",
            specialty="Remote Execution Node",
            skills=["python", "pytest", "workspace_exec"],
            divisions=["D10", "D12"],
            permissions=["workspace_exec", "run_backend_tests"],
            status="idle",
            provider_id="arena-agent",
            model_name="arena-agent-coder",
            workspace_path=workspace_root,
            device_id=device.id,
            autonomy_mode="supervised",
            git_branch=f"device/{device.id}",
        )
        db.add(agent)
    else:
        agent.status = "idle" if agent.status != "working" else agent.status
        agent.last_heartbeat = utcnow()
        if workspace_root:
            agent.workspace_path = workspace_root
        if worker_id:
            agent.permissions = sorted(set((agent.permissions or []) + [f"worker:{worker_id}"]))
    await db.flush()
    return agent


@router.post("/devices/{device_id}/register")
async def register_worker_device(
    device_id: str,
    body: dict,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Register or refresh a remote worker device and bind its server-side employee.

    Accepts either an admin JWT (an operator registering a node) or the device's own
    token (a daemon refreshing its registration after a server restart).
    """
    device = await db.get(ExecutionDevice, device_id)
    token_valid = device is not None and verify_device_token(
        device_id, x_aatm_device_token, device.token_version
    )
    if not token_valid:
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid or missing device token.")
        if user.role not in {"admin", "operator"}:
            raise HTTPException(status_code=403, detail="Insufficient permissions")

    created = device is None
    if device is None:
        device = ExecutionDevice(
            id=device_id,
            name=body.get("name") or f"Remote worker {device_id}",
            device_type=body.get("device_type") or "remote_server",
            host_address=body.get("host_address") or "unknown",
            os_name=body.get("os_name") or "unknown",
            cpu_cores=int(body.get("cpu_cores") or 2),
            ram_gb=float(body.get("ram_gb") or 4.0),
            capabilities=body.get("capabilities") or ["python3"],
            status="online",
            max_concurrent_tasks=int(body.get("max_concurrent_tasks") or 2),
        )
        db.add(device)
    else:
        device.name = body.get("name") or device.name
        device.device_type = body.get("device_type") or device.device_type
        device.host_address = body.get("host_address") or device.host_address
        device.os_name = body.get("os_name") or device.os_name
        device.capabilities = body.get("capabilities") or device.capabilities
        device.max_concurrent_tasks = int(body.get("max_concurrent_tasks") or device.max_concurrent_tasks)
        device.status = "online"
    device.last_heartbeat = utcnow()

    agent = await _ensure_bound_agent(
        db, device, workspace_root=body.get("workspace_root"), worker_id=body.get("worker_id")
    )

    db.add(
        AuditLog(
            actor=f"device:{device_id}",
            action="device.register",
            resource_type="execution_device",
            resource_id=device_id,
            details={"created": created, "worker_id": body.get("worker_id")},
        )
    )
    await db.commit()
    await db.refresh(device)

    hardened = not using_default_secret()
    return {
        "device": {
            "id": device.id,
            "name": device.name,
            "status": device.status,
            "device_type": device.device_type,
            "max_concurrent_tasks": device.max_concurrent_tasks,
            "last_heartbeat": device.last_heartbeat,
        },
        "bound_agent_id": agent.id,
        "device_token": issue_device_token(device_id, device.token_version),
        "token_hardened": hardened,
        "warning": None
        if hardened
        else "Server is still using the default development SECRET_KEY; device tokens are forgeable.",
    }


@router.post("/devices/{device_id}/revoke", response_model=DeviceTokenRevokeResponse)
async def revoke_device_token(
    device_id: str,
    user: User = Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """Atomically revoke prior device tokens; only admins and operators may rotate them."""
    result = await db.execute(
        update(ExecutionDevice)
        .where(ExecutionDevice.id == device_id)
        .values(token_version=ExecutionDevice.token_version + 1)
        .returning(ExecutionDevice.token_version)
    )
    token_version = result.scalar_one_or_none()
    if token_version is None:
        raise HTTPException(status_code=404, detail="Device not found")

    previous_version = token_version - 1
    db.add(
        AuditLog(
            actor=user.email,
            action="device.token_revoked",
            resource_type="execution_device",
            resource_id=device_id,
            details={"previous_token_version": previous_version, "token_version": token_version},
        )
    )
    await db.commit()
    await event_bus.emit(
        "device.token_revoked",
        {"device_id": device_id, "token_version": token_version, "actor": user.email},
    )
    return {
        "id": device_id,
        "token_version": token_version,
        "device_token": issue_device_token(device_id, token_version),
    }


@router.post("/devices/{device_id}/heartbeat")
async def worker_heartbeat(
    device_id: str,
    body: dict[str, Any] | None = None,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Liveness ping from a remote worker daemon or authenticated operator."""
    if x_aatm_device_token is not None:
        device = await require_device_token(device_id, x_aatm_device_token, db)
    else:
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=401,
                detail="Missing X-AATM-Device-Token or authenticated operator token",
            )
        device = await db.get(ExecutionDevice, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
    device.status = "online"
    device.last_heartbeat = utcnow()
    await db.commit()
    await event_bus.emit(
        "device.heartbeat",
        {
            "device_id": device_id,
            "telemetry": {
                k: body[k]
                for k in ("cpu_percent", "ram_percent")
                if isinstance(body, dict) and k in body
            },
        },
    )
    return {"id": device.id, "status": device.status, "last_heartbeat": device.last_heartbeat}


@router.post("/devices/{device_id}/claim")
async def claim_task_for_device(
    device_id: str,
    body: dict,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Claim the next eligible task for a remote device."""
    device = await require_device_token(device_id, x_aatm_device_token, db)

    worker_id = body.get("worker_id") or f"{device_id}-worker"

    # Respect the device concurrency budget. The row counter is reserved atomically
    # after a task claim below, so concurrent daemons cannot exceed this limit.
    active = list(
        (
            await db.execute(
                select(Task).where(
                    Task.device_id == device_id,
                    Task.status == "in_progress",
                )
            )
        ).scalars().all()
    )
    if len(active) >= device.max_concurrent_tasks:
        return {"task": None, "reason": "device concurrency limit reached"}

    res = await db.execute(
        select(Task)
        .where(Task.status == "queued")
        .order_by(Task.created_at.asc())
    )
    candidates = list(res.scalars().all())

    agent: Agent | None = None
    selected: Task | None = None
    claim_time = utcnow()
    for task in candidates:
        # A task pinned to another device is not ours to run.
        if task.device_id and task.device_id != device_id:
            continue
        if task.lock_owner and task.lock_owner != worker_id:
            continue
        if not await task_scheduler.dependencies_satisfied(db, task):
            continue

        if agent is None:
            agent = await _ensure_bound_agent(db, device)
            if not agent.is_enabled or agent.is_emergency_stopped:
                return {"task": None, "reason": "device worker employee is disabled or emergency-stopped"}

        claim_time = utcnow()
        checkpoints = list(task.checkpoints or []) + [
            {
                "ts": claim_time.isoformat(),
                "event": "device_claim",
                "device_id": device_id,
                "worker_id": worker_id,
            }
        ]
        claim_result = await db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == "queued",
                or_(Task.lock_owner.is_(None), Task.lock_owner == worker_id),
                or_(Task.device_id.is_(None), Task.device_id == device_id),
            )
            .values(
                status="in_progress",
                device_id=device_id,
                assigned_agent_id=agent.id,
                lock_owner=worker_id,
                locked_at=claim_time,
                started_at=func.coalesce(Task.started_at, claim_time),
                checkpoints=checkpoints,
            )
            .returning(Task.id)
            .execution_options(synchronize_session=False)
        )
        if claim_result.scalar_one_or_none() is None:
            continue

        await db.refresh(task)
        selected = task
        break

    if selected is None or agent is None:
        return {"task": None, "reason": "no eligible task"}

    # Pair the task CAS with an atomic device-slot reservation in this transaction.
    # If the device budget was consumed concurrently, rolling back also undoes the task claim.
    slot_result = await db.execute(
        update(ExecutionDevice)
        .where(
            ExecutionDevice.id == device_id,
            ExecutionDevice.active_tasks < ExecutionDevice.max_concurrent_tasks,
        )
        .values(
            active_tasks=ExecutionDevice.active_tasks + 1,
            last_heartbeat=claim_time,
        )
        .returning(ExecutionDevice.active_tasks)
        .execution_options(synchronize_session=False)
    )
    if slot_result.scalar_one_or_none() is None:
        await db.rollback()
        return {"task": None, "reason": "device concurrency limit reached"}

    agent.status = "working"
    agent.current_task_id = selected.id
    agent.last_heartbeat = claim_time
    db.add(
        AuditLog(
            actor=f"device:{device_id}",
            action="task.device_claimed",
            resource_type="task",
            resource_id=selected.id,
            details={"device_id": device_id, "worker_id": worker_id},
        )
    )
    await db.commit()
    await db.refresh(device)

    project = await db.get(Project, selected.project_id)
    await event_bus.emit("task.updated", {"task_id": selected.id, "status": selected.status})
    return {
        "task": {
            "id": selected.id,
            "project_id": selected.project_id,
            "division_code": selected.division_code,
            "title": selected.title,
            "description": selected.description,
            "priority": selected.priority,
            "expected_deliverables": selected.expected_deliverables or ["deliverable_report.md"],
            "execution_mode": selected.execution_mode,
            "device_id": device_id,
            "employee": {
                "id": agent.id,
                "name": agent.name,
                "role": agent.role,
                "role_template": agent.role_template,
                "specialty": agent.specialty,
                "skills": agent.skills or [],
                "permissions": agent.permissions or [],
                "autonomy_mode": agent.autonomy_mode,
            },
            "project": {
                "id": project.id if project else selected.project_id,
                "name": project.name if project else "",
                "description": project.description if project else "",
                "tech_stack": project.tech_stack if project else [],
            },
        },
        "priority_weight": PRIORITY_WEIGHT.get(selected.priority, 2),
    }


@router.post("/devices/{device_id}/tasks/{task_id}/model")
async def remote_agent_model_turn(
    device_id: str,
    task_id: str,
    body: DeviceAgentModelRequest,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Route a remote AgentLoop model turn through the central provider router."""
    await require_device_token(device_id, x_aatm_device_token, db)
    task, agent = await _require_claimed_task(db, device_id, task_id, body.worker_id)

    tree_paths: set[str] = set()
    tree = []
    for entry in body.workspace_file_tree:
        path = _validate_remote_path(entry.path)
        if path in tree_paths:
            raise HTTPException(status_code=422, detail="Remote workspace context contains duplicate paths.")
        tree_paths.add(path)
        tree.append({"path": path, "size_bytes": entry.size_bytes})

    context_files = []
    context_paths: set[str] = set()
    for entry in body.relevant_workspace_file_contents:
        path = _validate_remote_path(entry.path)
        if path in context_paths or path not in tree_paths:
            raise HTTPException(status_code=422, detail="Remote file context must contain unique paths listed in the workspace tree.")
        context_paths.add(path)
        content = safe_excerpt(entry.content, 3_000)
        content = sanitize_untrusted_content(content, source=f"remote_workspace:{path}")["quarantined_text"]
        context_files.append({"path": path, "content": content})

    previous_iterations = []
    for index, entry in enumerate(body.previous_iterations[-8:], start=1):
        clean_entry: dict[str, Any] = {
            "iteration": entry.iteration,
            "action": safe_excerpt(entry.action, 32),
            "result": sanitize_untrusted_content(
                safe_excerpt(entry.result, 4_000), source=f"remote_iteration:{index}"
            )["quarantined_text"],
        }
        if entry.correction:
            clean_entry["correction"] = sanitize_untrusted_content(
                safe_excerpt(entry.correction, 1_000), source=f"remote_correction:{index}"
            )["quarantined_text"]
        previous_iterations.append(clean_entry)

    correction = None
    if body.last_correction:
        correction = sanitize_untrusted_content(
            safe_excerpt(body.last_correction, 1_000), source="remote_last_correction"
        )["quarantined_text"]

    project = await db.get(Project, task.project_id)
    dependency_rows = []
    if task.dependencies:
        dependencies = await db.execute(select(Task).where(Task.id.in_(task.dependencies)))
        for dependency in dependencies.scalars().all():
            dependency_rows.append(
                {
                    "task_id": dependency.id,
                    "title": safe_excerpt(dependency.title, 240),
                    "status": dependency.status,
                    "summary": sanitize_untrusted_content(
                        safe_excerpt(dependency.result_summary or "", 2_000), source=f"dependency:{dependency.id}"
                    )["quarantined_text"],
                    "deliverables": list(dependency.actual_deliverables or []),
                }
            )

    prompt_data = {
        "employee": {
            "id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "role_template": agent.role_template,
            "specialty": agent.specialty,
            "skills": agent.skills or [],
            "permissions": agent.permissions or [],
            "autonomy_mode": agent.autonomy_mode,
        },
        "project": {
            "id": project.id if project else task.project_id,
            "name": project.name if project else "",
            "description": sanitize_untrusted_content(
                redact_secrets(project.description or "") if project else "", source="project:description"
            )["quarantined_text"],
            "tech_stack": project.tech_stack or [] if project else [],
        },
        "task": {
            "id": task.id,
            "title": sanitize_untrusted_content(task.title, source="task:title")["quarantined_text"],
            "description": sanitize_untrusted_content(task.description, source="task:description")["quarantined_text"],
            "instructions": sanitize_untrusted_content(task.description, source="task:instructions")["quarantined_text"],
            "priority": task.priority,
            "execution_mode": task.execution_mode,
            "expected_deliverables": task.expected_deliverables or ["deliverable_report.md"],
            "dependencies": task.dependencies or [],
        },
        "dependency_results": dependency_rows,
        "workspace_file_tree": tree,
        "relevant_workspace_file_contents": context_files,
        "previous_iterations": previous_iterations,
        "last_correction": correction,
        "action_protocol": REMOTE_ACTION_PROTOCOL,
    }

    spent, budget_limit = await _task_budget(db, task.id)
    if spent >= budget_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Per-task provider budget reached (${spent:.4f} of ${budget_limit:.4f}); no action was executed.",
        )

    request = ProviderRequest(
        prompt=json.dumps(prompt_data, ensure_ascii=False, default=str),
        system_prompt=build_system_prompt(
            agent.name,
            agent.role_template or agent.role,
            REMOTE_ACTION_PROTOCOL,
            remote_mode=True,
        ),
        model=agent.model_name,
        max_tokens=2_048,
        temperature=0.1,
        agent_id=agent.id,
        task_id=task.id,
        allow_diagnostic_fallback=False,
    )
    response = await provider_router.route_and_execute(db, request, preferred_provider_id=agent.provider_id)
    if not response.success:
        detail = safe_excerpt(response.error_message or "No model provider configured.", 1_000)
        status_code = 503 if "provider" in detail.casefold() or "credential" in detail.casefold() else 502
        raise HTTPException(status_code=status_code, detail=detail)

    spent, budget_limit = await _task_budget(db, task.id)
    if spent >= budget_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Per-task provider budget reached (${spent:.4f} of ${budget_limit:.4f}); returned action was not executed.",
        )
    return {"content": response.content, "provider_id": response.provider_id, "model": response.model}


@router.post("/devices/{device_id}/tasks/{task_id}/progress")
async def report_task_progress(
    device_id: str,
    task_id: str,
    body: DeviceTaskProgressRequest,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Persist one remote AgentLoop progress event and any action artifact."""
    device = await require_device_token(device_id, x_aatm_device_token, db)
    task, agent = await _require_claimed_task(db, device_id, task_id, body.worker_id)
    if not re.fullmatch(r"[a-z0-9_]{1,64}", body.event):
        raise HTTPException(status_code=422, detail="Progress event must be a lowercase identifier.")
    if body.artifact is not None:
        await _persist_remote_artifact(db, device_id, task, body.artifact)
    task.progress_percent = max(task.progress_percent, body.progress_percent)
    task.checkpoints = [
        *(task.checkpoints or []),
        {
            "ts": utcnow().isoformat(),
            "event": body.event,
            "iteration": body.iteration,
            "progress_percent": task.progress_percent,
            "summary": safe_excerpt(body.summary, 700),
            "device_id": device_id,
        },
    ][-200:]
    device.last_heartbeat = utcnow()
    agent.last_heartbeat = utcnow()
    await db.commit()
    await event_bus.emit(
        "task.updated",
        {"task_id": task.id, "status": task.status, "progress_percent": task.progress_percent},
    )
    return {"task_id": task.id, "progress_percent": task.progress_percent, "event": body.event}


@router.post("/devices/{device_id}/tasks/{task_id}/report")
async def report_task_result(
    device_id: str,
    task_id: str,
    body: DeviceTaskReportRequest,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Report the outcome of a task executed by a remote worker daemon."""
    device = await require_device_token(device_id, x_aatm_device_token, db)
    task, agent = await _require_claimed_task(db, device_id, task_id, body.worker_id)
    success = body.success
    summary = safe_excerpt(body.summary, 4_000)
    actual_deliverables = list(dict.fromkeys(body.actual_deliverables))
    for relative in actual_deliverables:
        _validate_remote_path(relative)
    expected = task.expected_deliverables or ["deliverable_report.md"]
    missing = sorted(set(expected) - set(actual_deliverables))
    if success and missing:
        success = False
        summary = f"Remote worker reported completion without verified deliverables: {', '.join(missing[:10])}."

    for artifact in body.artifacts:
        await _persist_remote_artifact(db, device_id, task, artifact)

    safe_trace = []
    for entry in body.trace[-100:]:
        if not isinstance(entry, dict):
            continue
        safe_trace.append(
            {
                "step": safe_excerpt(str(entry.get("step") or ""), 64),
                "action": safe_excerpt(str(entry.get("action") or ""), 32),
                "status": safe_excerpt(str(entry.get("status") or ""), 32),
                "exit_code": entry.get("exit_code") if isinstance(entry.get("exit_code"), int) else None,
                "elapsed_ms": entry.get("elapsed_ms") if isinstance(entry.get("elapsed_ms"), (int, float)) else None,
                "detail": safe_excerpt(str(entry.get("detail") or ""), 700),
            }
        )
    task.checkpoints = [
        *(task.checkpoints or []),
        {
            "ts": utcnow().isoformat(),
            "event": "device_task_result",
            "device_id": device_id,
            "worker_id": body.worker_id,
            "success": success,
            "summary": summary[:500],
            "trace": safe_trace,
            "keepalive_heartbeats": body.keepalive_heartbeats,
        },
    ][-200:]

    report_time = utcnow()
    released_count = await db.execute(
        update(ExecutionDevice)
        .where(ExecutionDevice.id == device_id, ExecutionDevice.active_tasks > 0)
        .values(
            active_tasks=ExecutionDevice.active_tasks - 1,
            last_heartbeat=report_time,
        )
        .returning(ExecutionDevice.active_tasks)
        .execution_options(synchronize_session=False)
    )
    remaining_slots = released_count.scalar_one_or_none()
    if remaining_slots is not None:
        device.active_tasks = remaining_slots
    device.last_heartbeat = report_time
    if success:
        task.status = "completed"
        task.progress_percent = 100
        task.result_summary = summary
        task.actual_deliverables = actual_deliverables
        task.completed_at = utcnow()
        task.lock_owner = None
        task.locked_at = None
        agent.tasks_completed += 1
    else:
        error = safe_excerpt(body.error_message or summary or "Remote execution failed", 2_000)
        task.retry_count += 1
        task.error_message = error
        task.lock_owner = None
        task.locked_at = None
        task.status = "queued" if task.retry_count <= task.max_retries else "failed"
        agent.tasks_failed += 1
    agent.status = "idle"
    agent.current_task_id = None
    agent.last_heartbeat = utcnow()

    db.add(
        AuditLog(
            actor=f"device:{device_id}",
            action="task.device_reported",
            resource_type="task",
            resource_id=task.id,
            details={"success": success, "summary": summary[:500], "worker_id": body.worker_id},
            severity=None if success else "warning",
        )
    )
    await db.commit()
    await event_bus.emit("task.updated", {"task_id": task.id, "status": task.status})
    return {"task_id": task.id, "status": task.status, "success": success, "missing_deliverables": missing if not success else []}


@router.get("/devices/{device_id}/status")
async def device_status(
    device_id: str,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Return the device record and the tasks it currently owns."""
    device = await require_device_token(device_id, x_aatm_device_token, db)
    tasks = list(
        (
            await db.execute(
                select(Task).where(Task.device_id == device_id, Task.status == "in_progress")
            )
        ).scalars().all()
    )
    return {
        "device": {
            "id": device.id,
            "name": device.name,
            "status": device.status,
            "last_heartbeat": device.last_heartbeat,
            "active_tasks": len(tasks),
        },
        "tasks": [{"id": t.id, "title": t.title, "status": t.status} for t in tasks],
    }


async def sweep_offline_devices(db: AsyncSession, actor: str = "self_healing") -> dict[str, Any]:
    """Mark silent devices as offline and requeue the tasks they owned.

    Called by the QA self-healing sweep: a worker host that died mid-task would
    otherwise leave its task in_progress forever.
    """
    cutoff = to_naive_utc(utcnow() - timedelta(minutes=DEVICE_OFFLINE_MINUTES))
    devices = list((await db.execute(select(ExecutionDevice))).scalars().all())
    offline: list[str] = []
    requeued: list[str] = []

    for device in devices:
        if device.id == "device-local":
            continue
        if device.status == "offline":
            continue
        heartbeat = to_naive_utc(device.last_heartbeat)
        if heartbeat is None or heartbeat > cutoff:
            continue
        device.status = "offline"
        device.active_tasks = 0
        offline.append(device.id)

        stuck = list(
            (
                await db.execute(
                    select(Task).where(Task.device_id == device.id, Task.status == "in_progress")
                )
            ).scalars().all()
        )
        for task in stuck:
            task.status = "queued"
            task.lock_owner = None
            task.locked_at = None
            task.device_id = None
            task.error_message = f"Device '{device.id}' went offline; task requeued by self-healing."
            task.checkpoints = list(task.checkpoints or []) + [
                {"ts": utcnow().isoformat(), "event": "device_offline", "device_id": device.id}
            ]
            requeued.append(task.id)

        agent = await db.get(Agent, _bound_agent_id(device.id))
        if agent:
            agent.status = "idle"
            agent.current_task_id = None

    if offline or requeued:
        db.add(
            AuditLog(
                actor=actor,
                action="device.offline_sweep",
                resource_type="execution_device",
                details={"offline_devices": offline, "requeued_tasks": requeued},
                severity="warning",
            )
        )
        await db.commit()
    return {"offline_devices": offline, "requeued_tasks": requeued, "cutoff_minutes": DEVICE_OFFLINE_MINUTES}


__all__ = ["router", "sweep_offline_devices", "issue_device_token", "get_settings"]
