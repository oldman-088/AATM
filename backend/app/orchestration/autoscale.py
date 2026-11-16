"""Phase 5 autoscaling: 1 manager + DEFAULT_ACTIVE_WORKERS default workers, approval-gated extras, idle scale-down.

* Backlog >= ``SCALE_UP_BACKLOG_THRESHOLD`` ready tasks that waited >= ``SCALE_UP_WAIT_SECONDS``
  with no idle worker -> one pending ``scale_up`` ApprovalRequest (``AUTO_SCALE_MODE=approval``),
  or direct creation (``automatic``), or nothing (``off``).
* Total workers never exceed ``MAX_CONCURRENT_WORKERS``.
* Extra workers idle for ``SCALE_DOWN_IDLE_MINUTES`` are disabled (never deleted, never defaults).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.models import Agent, ApprovalRequest, AuditLog, BrowserProfile, Notification, Task
from app.runtime.temporal import to_naive_utc

logger = logging.getLogger(__name__)

EXTRA_TEMPLATE = "autoscale_extra"
SCALE_ACTION = "scale_up"
DEFAULT_WORKER_IDS = ("worker-1", "worker-2", "worker-3", "worker-4")
TEMPLATE_WORKER_ID = "worker-1"


def is_extra(agent: Agent) -> bool:
    return agent.role_template == EXTRA_TEMPLATE


def database_backend() -> str:
    url = get_settings().DATABASE_URL or ""
    return "postgresql" if url.startswith(("postgresql", "postgres")) else ("sqlite" if "sqlite" in url else "other")


class AutoscaleService:
    async def _workers(self, db: AsyncSession) -> list[Agent]:
        return list((await db.execute(select(Agent).where(Agent.role == "worker"))).scalars().all())

    async def _ready_backlog(self, db: AsyncSession) -> list[Task]:
        from app.orchestration.scheduler import LOCAL_DEVICE_ID, task_scheduler

        queued = list(
            (
                await db.execute(
                    select(Task).where(Task.status == "queued", Task.task_type != "review")
                )
            ).scalars().all()
        )
        ready = []
        for t in queued:
            if t.device_id and t.device_id != LOCAL_DEVICE_ID:
                continue
            if t.execution_mode == "manual":
                continue  # pinned to a specific employee; more workers would not help
            if await task_scheduler.dependencies_satisfied(db, t):
                ready.append(t)
        return ready

    async def status(self, db: AsyncSession) -> dict[str, Any]:
        settings = get_settings()
        workers = await self._workers(db)
        enabled = [w for w in workers if w.is_enabled]
        extras = [w for w in enabled if is_extra(w)]
        backlog = await self._ready_backlog(db)
        pending = await self._pending_request(db)
        db_backend = database_backend()
        employees = len(enabled) + 1
        return {
            "mode": settings.AUTO_SCALE_MODE,
            "default_workers": len([w for w in enabled if not is_extra(w)]),
            "extra_workers": len(extras),
            "total_workers": len(enabled),
            "max_workers": settings.MAX_CONCURRENT_WORKERS,
            "parallel_slots": settings.AGENT_MAX_PARALLEL,
            "idle_workers": len([w for w in enabled if w.status == "idle" and not w.is_emergency_stopped]),
            "ready_backlog": len(backlog),
            "scale_up_threshold": settings.SCALE_UP_BACKLOG_THRESHOLD,
            "scale_up_wait_seconds": settings.SCALE_UP_WAIT_SECONDS,
            "scale_down_idle_minutes": settings.SCALE_DOWN_IDLE_MINUTES,
            "pending_approval_id": pending.id if pending else None,
            "database_backend": db_backend,
            "sqlite_warning": db_backend == "sqlite" and employees > settings.SQLITE_EMPLOYEE_WARNING_THRESHOLD,
            "label": f"{settings.DEFAULT_ACTIVE_WORKERS} default + {len(extras)} extra",
        }

    async def _pending_request(self, db: AsyncSession) -> ApprovalRequest | None:
        return (
            await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.action_type == SCALE_ACTION, ApprovalRequest.status == "pending")
                .limit(1)
            )
        ).scalar_one_or_none()

    async def evaluate(self, db: AsyncSession) -> dict[str, Any]:
        """Run once per orchestration cycle (after dispatch). Returns what it did."""
        settings = get_settings()
        result: dict[str, Any] = {"mode": settings.AUTO_SCALE_MODE, "proposed": 0, "created": [], "retired": []}
        if settings.AUTO_SCALE_MODE == "off":
            return result
        result["retired"] = await self.scale_down(db)

        workers = [w for w in await self._workers(db) if w.is_enabled]
        idle = [w for w in workers if w.status == "idle" and not w.is_emergency_stopped]
        backlog = await self._ready_backlog(db)
        if idle or len(backlog) < settings.SCALE_UP_BACKLOG_THRESHOLD:
            return result
        now = to_naive_utc(utcnow())
        waited = [t for t in backlog if (now - to_naive_utc(t.created_at)).total_seconds() >= settings.SCALE_UP_WAIT_SECONDS]
        if len(waited) < settings.SCALE_UP_BACKLOG_THRESHOLD:
            return result
        headroom = settings.MAX_CONCURRENT_WORKERS - len(workers)
        count = min(settings.SCALE_UP_STEP, headroom, len(backlog))
        if count <= 0:
            result["at_capacity"] = True
            return result
        if settings.AUTO_SCALE_MODE == "automatic":
            created = await self.add_workers(db, count, actor="autoscaler", reason=f"backlog={len(backlog)}")
            result["created"] = [a.id for a in created]
            return result
        if await self._pending_request(db) is not None:
            result["pending"] = True
            return result
        approval = ApprovalRequest(
            action_type=SCALE_ACTION,
            title=f"Add {count} extra AI employee(s)",
            description=(
                f"{len(backlog)} ready tasks have waited at least {int(settings.SCALE_UP_WAIT_SECONDS)}s and all "
                f"{len(workers)} workers are busy. Approve to add {count} extra worker(s) "
                f"(limit {settings.MAX_CONCURRENT_WORKERS}). Extra workers increase provider spend."
            ),
            requested_by="autoscaler",
            payload={"count": count, "backlog": len(backlog), "workers": len(workers), "kind": "spend"},
        )
        db.add(approval)
        db.add(
            Notification(
                category="scaling",
                severity="warning",
                title="Scale-up proposed",
                message=f"{len(backlog)} tasks waiting; approve adding {count} worker(s).",
                link="/approvals",
            )
        )
        await db.commit()
        result["proposed"] = count
        result["approval_id"] = approval.id
        await event_bus.emit("scaling.proposed", {"approval_id": approval.id, "count": count, "backlog": len(backlog)})
        return result

    async def on_approval_decided(self, db: AsyncSession, approval: ApprovalRequest, *, actor: str) -> dict[str, Any] | None:
        if approval.action_type != SCALE_ACTION:
            return None
        payload = dict(approval.payload or {})
        if approval.status != "approved":
            return {"ok": False, "status": approval.status}
        if payload.get("executed"):
            return {"ok": False, "code": "APPROVAL_ALREADY_USED"}
        created = await self.add_workers(db, int(payload.get("count") or 1), actor=actor, reason=f"approval:{approval.id}")
        payload["executed"] = True
        payload["created"] = [a.id for a in created]
        approval.payload = payload
        await db.commit()
        return {"ok": True, "created": payload["created"]}

    async def add_workers(self, db: AsyncSession, count: int, *, actor: str, reason: str) -> list[Agent]:
        settings = get_settings()
        workers = await self._workers(db)
        enabled = [w for w in workers if w.is_enabled]
        count = max(0, min(count, settings.MAX_CONCURRENT_WORKERS - len(enabled)))
        template = next((w for w in workers if w.id == TEMPLATE_WORKER_ID), None) or next(iter(workers), None)
        created: list[Agent] = []
        # Re-enable retired extras first, then create new ones.
        retired = sorted([w for w in workers if is_extra(w) and not w.is_enabled], key=lambda w: w.id)
        for agent in retired[:count]:
            agent.is_enabled = True
            agent.status = "idle"
            agent.current_task_id = None
            agent.last_heartbeat = utcnow()
            created.append(agent)
        existing_ids = {w.id for w in workers}
        n = 1
        while len(created) < count:
            while f"worker-x{n}" in existing_ids:
                n += 1
            agent_id = f"worker-x{n}"
            existing_ids.add(agent_id)
            profile_id = f"profile-{agent_id}"
            if await db.get(BrowserProfile, profile_id) is None:
                db.add(
                    BrowserProfile(
                        id=profile_id,
                        name=f"Extra worker {n} profile",
                        agent_id=agent_id,
                        profile_dir=str(settings.BROWSER_PROFILES_DIR / profile_id),
                        mode="background",
                        status="ready",
                    )
                )
            agent = Agent(
                id=agent_id,
                name=f"Extra AI Engineer {n}",
                role="worker",
                reports_to="manager-0",
                role_template=EXTRA_TEMPLATE,
                specialty=template.specialty if template else "General software engineering",
                skills=list(template.skills) if template else [],
                divisions=list(template.divisions) if template else [],
                permissions=list(template.permissions) if template else [],
                status="idle",
                provider_id=template.provider_id if template else None,
                model_name=template.model_name if template else "claude-3-7-sonnet-latest",
                browser_profile_id=profile_id,
                device_id="device-local",
                autonomy_mode=template.autonomy_mode if template else "semi_autonomous",
            )
            db.add(agent)
            created.append(agent)
        if created:
            db.add(
                AuditLog(
                    actor=actor,
                    action="scaling.workers_added",
                    resource_type="agent",
                    resource_id=",".join(a.id for a in created),
                    details={"count": len(created), "reason": reason},
                )
            )
        await db.commit()
        if created:
            await event_bus.emit("scaling.workers_added", {"agents": [a.id for a in created], "reason": reason})
        return created

    async def _last_activity(self, db: AsyncSession, agent: Agent):
        last_task = (
            await db.execute(select(func.max(Task.updated_at)).where(Task.assigned_agent_id == agent.id))
        ).scalar_one_or_none()
        candidates = [to_naive_utc(x) for x in (last_task, agent.created_at) if x is not None]
        return max(candidates) if candidates else None

    async def scale_down(self, db: AsyncSession, *, now=None) -> list[str]:
        settings = get_settings()
        now = to_naive_utc(now or utcnow())
        cutoff = now - timedelta(minutes=settings.SCALE_DOWN_IDLE_MINUTES)
        retired: list[str] = []
        for agent in await self._workers(db):
            if not is_extra(agent) or not agent.is_enabled or agent.status != "idle" or agent.current_task_id:
                continue
            last = await self._last_activity(db, agent)
            if last is not None and last <= cutoff:
                agent.is_enabled = False
                agent.status = "offline"
                retired.append(agent.id)
        if retired:
            db.add(
                AuditLog(
                    actor="autoscaler",
                    action="scaling.workers_retired",
                    resource_type="agent",
                    resource_id=",".join(retired),
                    details={"idle_minutes": settings.SCALE_DOWN_IDLE_MINUTES},
                )
            )
            await db.commit()
            await event_bus.emit("scaling.workers_retired", {"agents": retired})
        return retired


autoscale_service = AutoscaleService()
