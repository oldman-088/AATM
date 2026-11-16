from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.config import get_settings
from app.core.events import event_bus
from app.core.security import redact_sensitive_mapping
from app.db.base import utcnow
from app.db.session import get_db
from app.models import (
    Agent,
    AIProvider,
    ApprovalRequest,
    AuditLog,
    BrowserProfile,
    ExecutionDevice,
    Notification,
    SystemSetting,
    Task,
    User,
)
from app.providers.router import get_model_configuration_status
from app.qa.engine import qa_engine
from app.runtime.sandbox import get_sandbox_status
from app.schemas.common import ApprovalCreate, ApprovalDecision, SettingUpdate

router = APIRouter(tags=["governance-qa-dashboard"], dependencies=[Depends(get_current_user)])


@router.get("/dashboard/overview")
async def dashboard_overview(db: AsyncSession = Depends(get_db)):
    agents = list((await db.execute(select(Agent))).scalars().all())
    tasks = list((await db.execute(select(Task))).scalars().all())
    providers = list((await db.execute(select(AIProvider))).scalars().all())
    browsers = list((await db.execute(select(BrowserProfile))).scalars().all())
    pending_approvals = list(
        (await db.execute(select(ApprovalRequest).where(ApprovalRequest.status == "pending"))).scalars().all()
    )
    intel = await qa_engine.compute_team_intelligence(db)

    division_progress = {}
    for t in tasks:
        entry = division_progress.setdefault(t.division_code, {"total": 0, "completed": 0, "progress_sum": 0})
        entry["total"] += 1
        if t.status == "completed":
            entry["completed"] += 1
        entry["progress_sum"] += t.progress_percent

    divisions = []
    for code in [f"D{i:02d}" for i in range(1, 13)]:
        d = division_progress.get(code, {"total": 0, "completed": 0, "progress_sum": 0})
        pct = round(d["progress_sum"] / d["total"], 1) if d["total"] else 0.0
        divisions.append({"code": code, "total_tasks": d["total"], "completed_tasks": d["completed"], "progress_percent": pct})

    return {
        "kpis": {
            "total_agents": len(agents),
            "active_workers": sum(1 for a in agents if a.role == "worker" and a.status == "working"),
            "idle_workers": sum(1 for a in agents if a.role == "worker" and a.status == "idle"),
            "total_tasks": len(tasks),
            "completed_tasks": sum(1 for t in tasks if t.status == "completed"),
            "completion_rate": intel["completion_rate"],
            "healthy_providers": sum(1 for p in providers if p.health_status == "healthy"),
            "total_providers": len(providers),
            "running_browsers": sum(1 for b in browsers if b.status == "running"),
            "total_browsers": len(browsers),
            "pending_approvals": len(pending_approvals),
        },
        "divisions": divisions,
        "resources": intel["resources"],
        "recommendations": intel["recommendations"],
        "recent_events": event_bus.recent_events(15),
        "sandbox": get_sandbox_status(),
        "model_status": await get_model_configuration_status(db),
    }


@router.get("/approvals")
async def list_approvals(status: str | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(ApprovalRequest)
    if status:
        stmt = stmt.where(ApprovalRequest.status == status)
    res = await db.execute(stmt.order_by(ApprovalRequest.created_at.desc()))
    return list(res.scalars().all())


@router.post("/approvals", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def create_approval(
    body: ApprovalCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    app = ApprovalRequest(
        project_id=body.project_id,
        action_type=body.action_type,
        title=body.title,
        description=body.description,
        requested_by=user.email,
        estimated_cost_usd=body.estimated_cost_usd,
        payload=redact_sensitive_mapping(body.payload),
        status="pending",
    )
    db.add(app)
    await db.commit()
    await db.refresh(app)
    await event_bus.emit("approval.requested", {"id": app.id, "title": app.title})
    return app


@router.post("/approvals/{approval_id}/decide", dependencies=[Depends(require_role("admin", "operator"))])
async def decide_approval(
    approval_id: str,
    body: ApprovalDecision,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    app = await db.get(ApprovalRequest, approval_id)
    if not app:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if app.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Approval request '{approval_id}' has already been decided (status='{app.status}').",
        )
    app.status = body.decision
    app.reviewed_by = user.email
    app.review_note = body.review_note
    app.decided_at = utcnow()
    db.add(
        AuditLog(
            actor=user.email,
            action=f"approval.{body.decision}",
            resource_type="approval_request",
            resource_id=app.id,
            details={"action_type": app.action_type, "note": body.review_note},
        )
    )
    await db.commit()
    await db.refresh(app)
    if app.action_type == "merge":
        from app.orchestration.review import review_service

        merge_result = await review_service.on_approval_decided(db, app, actor=user.email)
        await db.refresh(app)
        if merge_result is not None:
            await event_bus.emit("approval.merge_result", {"approval_id": app.id, **merge_result})
    return app


@router.get("/audit")
async def list_audit_logs(limit: int = 80, db: AsyncSession = Depends(get_db)):
    bounded_limit = max(1, min(int(limit), 500))
    res = await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(bounded_limit))
    rows = list(res.scalars().all())
    return [
        {
            "id": row.id,
            "actor": row.actor,
            "action": row.action,
            "severity": row.severity,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "details": redact_sensitive_mapping(row.details),
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/governance/security-posture")
async def governance_security_posture(db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    users = list((await db.execute(select(User))).scalars().all())
    approvals = list((await db.execute(select(ApprovalRequest).where(ApprovalRequest.status == "pending"))).scalars().all())
    warnings = list(
        (await db.execute(select(AuditLog).where(AuditLog.severity.in_(["warning", "critical"])))).scalars().all()
    )
    devices = list((await db.execute(select(ExecutionDevice))).scalars().all())
    return {
        "environment": settings.ENVIRONMENT,
        "sandbox": get_sandbox_status(),
        "model_status": await get_model_configuration_status(db),
        "users_requiring_password_change": sum(1 for u in users if u.must_change_password),
        "pending_approvals": len(approvals),
        "security_warnings_count": len(warnings),
        "active_devices": sum(1 for d in devices if d.status in {"online", "paired", "active"}),
        "offline_or_degraded_devices": sum(1 for d in devices if d.status in {"offline", "disconnected", "degraded", "revoked"}),
        "headers_enforced": [
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Referrer-Policy",
            "Permissions-Policy",
            "Content-Security-Policy",
        ],
    }


@router.get("/notifications")
async def list_notifications(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Notification).order_by(Notification.created_at.desc()).limit(50))
    return list(res.scalars().all())


@router.post("/notifications/{notif_id}/read", dependencies=[Depends(require_role("admin", "operator"))])
async def mark_notification_read(
    notif_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notif = await db.get(Notification, notif_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    notif.is_read = True
    await db.commit()
    return {"ok": True}


@router.get("/qa/summary")
async def qa_summary(db: AsyncSession = Depends(get_db)):
    return {
        "security_scan": qa_engine.run_security_scan(),
        "intelligence": await qa_engine.compute_team_intelligence(db),
    }


@router.post("/qa/self-heal", dependencies=[Depends(require_role("admin", "operator"))])
async def trigger_self_healing(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await qa_engine.run_self_healing(db)


@router.get("/settings")
async def list_settings(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(SystemSetting))
    return {s.key: {"value": s.value, "description": s.description, "updated_at": s.updated_at} for s in res.scalars().all()}


@router.put("/settings/{key}", dependencies=[Depends(require_role("admin", "operator"))])
async def update_setting(
    key: str,
    body: SettingUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    setting = await db.get(SystemSetting, key)
    if setting:
        setting.value = body.value
        if body.description is not None:
            setting.description = body.description
    else:
        setting = SystemSetting(key=key, value=body.value, description=body.description)
        db.add(setting)
    db.add(AuditLog(actor=user.email, action="setting.update", resource_type="setting", resource_id=key, details=body.value))
    await db.commit()
    return {"key": key, "value": setting.value}
