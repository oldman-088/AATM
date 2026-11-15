"""Multi-device execution API for remote AATM worker daemons.

The central server keeps the authoritative `ExecutionDevice` registry and the task
queue. A remote worker daemon registers itself, heartbeats, claims a task and reports
the outcome. Every endpoint except `GET /devices` is authenticated with a device-scoped
token (see `app.runtime.device_auth`).
"""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.db.session import get_db
from app.models import Agent, AuditLog, ExecutionDevice, Task, User, WorkspaceArtifact
from app.orchestration.scheduler import PRIORITY_WEIGHT, task_scheduler
from app.runtime.device_auth import issue_device_token, using_default_secret, verify_device_token
from app.runtime.temporal import to_naive_utc

router = APIRouter(tags=["multi-device-execution"])

# A device that has not been heard from within this window is considered disconnected
# and the tasks it owned are requeued.
DEVICE_OFFLINE_MINUTES = 5


def _bound_agent_id(device_id: str) -> str:
    """Deterministic employee id that represents a device on the server."""
    return f"dev-{device_id}"[:64]


def require_device_token(device_id: str, token: str | None) -> None:
    """Raise 401 unless the supplied token is valid for this device."""
    if not verify_device_token(device_id, token):
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")


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
    token_valid = verify_device_token(device_id, x_aatm_device_token)
    if not token_valid and user is None:
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")

    device = await db.get(ExecutionDevice, device_id)
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
        "device_token": issue_device_token(device_id),
        "token_hardened": hardened,
        "warning": None
        if hardened
        else "Server is still using the default development SECRET_KEY; device tokens are forgeable.",
    }


@router.post("/devices/{device_id}/heartbeat")
async def worker_heartbeat(
    device_id: str,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Liveness ping from a remote worker daemon."""
    require_device_token(device_id, x_aatm_device_token)
    device = await db.get(ExecutionDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    device.status = "online"
    device.last_heartbeat = utcnow()
    await db.commit()
    await event_bus.emit("device.heartbeat", {"device_id": device_id})
    return {"id": device.id, "status": device.status, "last_heartbeat": device.last_heartbeat}


@router.post("/devices/{device_id}/claim")
async def claim_task_for_device(
    device_id: str,
    body: dict,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Claim the next eligible task for a remote device."""
    require_device_token(device_id, x_aatm_device_token)
    device = await db.get(ExecutionDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    worker_id = body.get("worker_id") or f"{device_id}-worker"

    # Respect the device concurrency budget.
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

    selected = None
    for task in candidates:
        # A task pinned to another device is not ours to run.
        if task.device_id and task.device_id != device_id:
            continue
        if task.lock_owner and task.lock_owner != worker_id:
            continue
        if not await task_scheduler.dependencies_satisfied(db, task):
            continue
        selected = task
        break

    if selected is None:
        return {"task": None, "reason": "no eligible task"}

    agent = await _ensure_bound_agent(db, device)
    selected.status = "in_progress"
    selected.device_id = device_id
    selected.assigned_agent_id = agent.id
    selected.lock_owner = worker_id
    selected.locked_at = utcnow()
    selected.started_at = selected.started_at or utcnow()
    selected.checkpoints = list(selected.checkpoints or []) + [
        {
            "ts": utcnow().isoformat(),
            "event": "device_claim",
            "device_id": device_id,
            "worker_id": worker_id,
        }
    ]
    device.active_tasks = len(active) + 1
    device.last_heartbeat = utcnow()
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
        },
        "priority_weight": PRIORITY_WEIGHT.get(selected.priority, 2),
    }


@router.post("/devices/{device_id}/tasks/{task_id}/report")
async def report_task_result(
    device_id: str,
    task_id: str,
    body: dict,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Report the outcome of a task executed by a remote worker daemon."""
    require_device_token(device_id, x_aatm_device_token)
    device = await db.get(ExecutionDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.device_id and task.device_id != device_id:
        raise HTTPException(status_code=409, detail="Task is not owned by this device.")

    worker_id = body.get("worker_id") or f"{device_id}-worker"
    success = bool(body.get("success"))
    summary = body.get("summary") or ""

    for art in body.get("artifacts") or []:
        db.add(
            WorkspaceArtifact(
                agent_id=_bound_agent_id(device_id),
                project_id=task.project_id,
                task_id=task.id,
                relative_path=art.get("relative_path", "."),
                action=art.get("action", "create"),
                summary=art.get("summary") or f"Remote artifact from {device_id}",
                exit_code=int(art.get("exit_code") or 0),
            )
        )

    device.active_tasks = max(0, device.active_tasks - 1)
    device.last_heartbeat = utcnow()

    if success:
        task.status = "completed"
        task.progress_percent = 100
        task.result_summary = summary
        task.completed_at = utcnow()
        task.lock_owner = None
        task.locked_at = None
        task.actual_deliverables = [a.get("relative_path") for a in (body.get("artifacts") or []) if a.get("relative_path")]
        agent = await db.get(Agent, _bound_agent_id(device_id))
        if agent:
            agent.tasks_completed += 1
            agent.status = "idle"
            agent.current_task_id = None
            agent.last_heartbeat = utcnow()
    else:
        error = body.get("error_message") or summary or "Remote execution failed"
        task.retry_count += 1
        task.error_message = error
        task.lock_owner = None
        task.locked_at = None
        if task.retry_count <= task.max_retries:
            task.status = "queued"
        else:
            task.status = "failed"
        agent = await db.get(Agent, _bound_agent_id(device_id))
        if agent:
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
            details={"success": success, "summary": summary[:500], "worker_id": worker_id},
            severity=None if success else "warning",
        )
    )
    await db.commit()
    await event_bus.emit("task.updated", {"task_id": task.id, "status": task.status})
    return {"task_id": task.id, "status": task.status, "success": success}


@router.get("/devices/{device_id}/status")
async def device_status(
    device_id: str,
    x_aatm_device_token: str | None = Header(default=None, alias="X-AATM-Device-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Return the device record and the tasks it currently owns."""
    require_device_token(device_id, x_aatm_device_token)
    device = await db.get(ExecutionDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
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
