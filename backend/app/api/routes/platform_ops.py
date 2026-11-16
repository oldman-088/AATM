"""Execution devices, emergency stop and untrusted-text sanitization."""


from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.security import sanitize_untrusted_content
from app.db.base import utcnow
from app.db.session import get_db
from app.models import (
    ExecutionDevice,
    User,
)
from app.orchestration.replacement import replacement_service
from app.runtime.device_auth import issue_device_token
from app.schemas.common import (
    DeviceRegisterRequest,
    EmergencyStopRequest,
    SanitizeRequest,
)

router = APIRouter(tags=["enterprise-platform"], dependencies=[Depends(get_current_user)])


@router.get("/devices")
async def list_devices(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ExecutionDevice).order_by(ExecutionDevice.registered_at.asc()))).scalars().all()
    return list(rows)


@router.post("/devices", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
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


@router.post("/emergency-stop", dependencies=[Depends(require_role("admin", "operator"))])
async def trigger_emergency_stop(
    body: EmergencyStopRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await replacement_service.emergency_stop(
        db, scope=body.scope, target_id=body.target_id, actor=user.email, reason=body.reason
    )


@router.post("/emergency-stop/clear", dependencies=[Depends(require_role("admin", "operator"))])
async def clear_emergency_stop(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await replacement_service.clear_emergency_stop(db, actor=user.email)


@router.post("/security/sanitize", dependencies=[Depends(require_role("admin", "operator"))])
async def sanitize_external_text(body: SanitizeRequest):
    return sanitize_untrusted_content(body.text, source=body.source)
