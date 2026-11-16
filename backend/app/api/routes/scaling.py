"""Phase 5 API: team size, autoscaling status, approval-gated "Add employee", broker/coordination status."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.coordination import get_coordinator
from app.core.config import get_settings
from app.db.session import get_db
from app.models import ApprovalRequest, AuditLog, User
from app.orchestration.autoscale import SCALE_ACTION, autoscale_service
from app.providers.concurrency import concurrency_status
from app.queue import wake_bus_status

router = APIRouter(tags=["scaling"], dependencies=[Depends(get_current_user)])


class AddEmployeesBody(BaseModel):
    count: int = Field(default=1, ge=1, le=25)
    reason: str = Field(default="", max_length=500)


@router.get("/scaling/status")
async def scaling_status(db: AsyncSession = Depends(get_db)):
    status = await autoscale_service.status(db)
    status["queue"] = wake_bus_status()
    status["coordination"] = get_coordinator().status()
    status["provider_concurrency"] = concurrency_status()
    return status


@router.post("/scaling/workers", status_code=202, dependencies=[Depends(require_role("admin", "operator"))])
async def request_extra_workers(
    body: AddEmployeesBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """"Add employee": opens a spend ApprovalRequest; workers are created only when a human approves it."""
    settings = get_settings()
    approval = ApprovalRequest(
        id=str(uuid.uuid4()),
        action_type=SCALE_ACTION,
        title=f"Add {body.count} extra AI employee(s)",
        description=(body.reason or "Manual request from the dashboard.")
        + f" Limit {settings.MAX_CONCURRENT_WORKERS} workers. Extra workers increase provider spend.",
        requested_by=user.email,
        payload={"count": body.count, "kind": "spend", "manual": True},
    )
    db.add(approval)
    db.add(
        AuditLog(
            actor=user.email,
            action="scaling.requested",
            resource_type="approval",
            resource_id=approval.id,
            details={"count": body.count},
        )
    )
    await db.commit()
    return {"approval_id": approval.id, "status": approval.status, "count": body.count}


@router.post("/scaling/scale-down", dependencies=[Depends(require_role("admin", "operator"))])
async def run_scale_down(db: AsyncSession = Depends(get_db)):
    return {"retired": await autoscale_service.scale_down(db)}
