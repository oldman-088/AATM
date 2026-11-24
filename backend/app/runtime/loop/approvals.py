"""Emergency stop, budget limits, approval pauses, needs_human blocking and failure handling for AgentLoop."""

from __future__ import annotations

import math

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.models import (
    Agent,
    AgentMessage,
    ApprovalRequest,
    AuditLog,
    ProviderUsageLog,
    SystemSetting,
    Task,
)
from app.orchestration.scheduler import task_scheduler
from app.runtime.agent_protocol import (
    ApprovalAction,
)
from app.runtime.agent_protocol import (
    safe_excerpt as _safe_excerpt,
)
from app.runtime.loop.common import (
    NeedsHumanError,
    RepoAccessError,
    _block_for_human_impl,
    _requeue_for_repo_access_impl,
)


class ApprovalsMixin:
    """Mixin for AgentLoop; relies on attributes set in AgentLoop.__init__."""

    async def _is_emergency_stopped(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
    ) -> bool:
        await db.refresh(agent)
        await db.refresh(task)
        await db.refresh(message)
        return bool(
            agent.is_emergency_stopped
            or not agent.is_enabled
            or task.status != "in_progress"
            or (task.error_message or "").startswith("Emergency stop")
        )

    async def _pause_for_stop(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
        reason: str,
        iteration: int | None = None,
    ) -> None:
        payload = dict(message.response_payload or {})
        payload.pop("approval_id", None)
        payload.pop("pending_approval_id", None)
        payload["paused_reason"] = reason
        message.response_payload = payload
        message.execution_status = "paused"
        message.claimed_by = None
        message.claimed_at = None
        if task.status == "in_progress":
            task.status = "blocked"
            task.lock_owner = None
            task.locked_at = None
            task.error_message = f"Emergency stop: {reason}"
            task.checkpoints = list(task.checkpoints or []) + [
                {"ts": utcnow().isoformat(), "event": "agent_loop_paused", "reason": reason}
            ]
        if agent.current_task_id == task.id:
            agent.current_task_id = None
            agent.status = "paused" if agent.is_emergency_stopped else "idle"
        if iteration is not None:
            task.checkpoints = list(task.checkpoints or []) + [
                {"ts": utcnow().isoformat(), "event": "agent_iteration_stopped", "iteration": iteration}
            ]
        agent.last_heartbeat = utcnow()
        await db.commit()
        await event_bus.emit("task.updated", {"task_id": task.id, "status": task.status, "paused": True})
        await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status})

    async def _task_cost_and_limit(self, db: AsyncSession, task_id: str) -> tuple[float, float]:
        spent = float(
            (
                await db.execute(
                    select(func.coalesce(func.sum(ProviderUsageLog.estimated_cost_usd), 0.0)).where(
                        ProviderUsageLog.task_id == task_id
                    )
                )
            ).scalar_one()
            or 0.0
        )
        default_limit = float(get_settings().AGENT_TASK_BUDGET_USD)
        budgets = await db.get(SystemSetting, "budgets")
        configured = budgets.value.get("per_task_limit_usd") if budgets and isinstance(budgets.value, dict) else None
        try:
            limit = float(configured) if configured is not None else default_limit
        except (TypeError, ValueError):
            limit = default_limit
        if not math.isfinite(limit) or limit < 0:
            limit = default_limit
        return spent, limit

    async def _pause_for_approval(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
        action: ApprovalAction,
        iteration: int,
        result: str,
    ) -> None:
        payload = dict(message.response_payload or {})
        approval_id = payload.pop("pending_approval_id", None)
        approval = await db.get(ApprovalRequest, str(approval_id)) if approval_id else None
        if approval is None:
            raise RuntimeError("Approval action did not persist an ApprovalRequest.")
        payload["approval_id"] = approval.id
        message.response_payload = payload
        message.execution_status = "paused"
        message.claimed_by = None
        message.claimed_at = None
        task.status = "blocked"
        task.lock_owner = None
        task.locked_at = None
        task.checkpoints = list(task.checkpoints or []) + [
            {
                "ts": utcnow().isoformat(),
                "event": "approval_requested",
                "approval_id": approval.id,
                "iteration": iteration,
                "action_type": action.action_type,
            }
        ]
        if agent.current_task_id == task.id:
            agent.current_task_id = None
            agent.status = "idle"
        agent.last_heartbeat = utcnow()
        await self._persist_iteration(
            db,
            message,
            task,
            agent,
            iteration,
            "approval",
            result,
        )
        db.add(
            AuditLog(
                actor=agent.id,
                action="agent.approval_requested",
                resource_type="approval_request",
                resource_id=approval.id,
                details={"task_id": task.id, "action_type": action.action_type},
            )
        )
        await db.commit()
        await event_bus.emit("task.updated", {"task_id": task.id, "status": "blocked", "approval_id": approval.id})
        await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status, "task_id": task.id})

    async def resume_approval_messages(self) -> int:
        """Resume approved messages after a decision; rejected requests cancel their task."""
        resumed = 0
        async with self.session_factory() as db:
            result = await db.execute(select(AgentMessage).where(AgentMessage.execution_status == "paused"))
            messages = list(result.scalars().all())
            for message in messages:
                payload = dict(message.response_payload or {})
                approval_id = payload.get("approval_id")
                if not approval_id:
                    continue
                approval = await db.get(ApprovalRequest, str(approval_id))
                if not approval or approval.status == "pending":
                    continue
                task = await db.get(Task, message.task_id) if message.task_id else None
                agent = await db.get(Agent, message.recipient_id)
                if task is None or agent is None:
                    continue
                if approval.status == "approved":
                    if agent.is_emergency_stopped or not agent.is_enabled:
                        continue
                    if agent.status == "working" and agent.current_task_id != task.id:
                        continue
                    if not await task_scheduler.acquire_lock(db, task, agent.id):
                        continue
                    task.status = "in_progress"
                    task.error_message = None
                    message.execution_status = "running"
                    message.claimed_by = None
                    message.claimed_at = None
                    payload["approval_decision"] = {"status": "approved", "review_note": approval.review_note}
                    message.response_payload = payload
                    agent.status = "working"
                    agent.current_task_id = task.id
                    agent.last_heartbeat = utcnow()
                    db.add(
                        AuditLog(
                            actor=approval.reviewed_by or "approval-center",
                            action="agent.approval_resumed",
                            resource_type="task",
                            resource_id=task.id,
                            details={"approval_id": approval.id, "agent_id": agent.id},
                        )
                    )
                    await db.commit()
                    await event_bus.emit("task.updated", {"task_id": task.id, "status": "in_progress", "approval_id": approval.id})
                    await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status, "task_id": task.id})
                    resumed += 1
                elif approval.status == "rejected":
                    task.status = "cancelled"
                    task.error_message = f"Approval rejected: {approval.review_note or approval.title}"
                    task.lock_owner = None
                    task.locked_at = None
                    message.execution_status = "rejected"
                    payload["approval_decision"] = {"status": "rejected", "review_note": approval.review_note}
                    message.response_payload = payload
                    message.claimed_by = None
                    message.claimed_at = None
                    if agent.current_task_id == task.id:
                        agent.current_task_id = None
                        agent.status = "idle"
                    agent.last_heartbeat = utcnow()
                    await db.commit()
                    await self._store_outcome_memory(db, task, agent, "cancelled", task.error_message)
                    await event_bus.emit("task.updated", {"task_id": task.id, "status": "cancelled", "approval_id": approval.id})
                    await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status})
        return resumed

    async def _block_for_human(self, db: AsyncSession, message: AgentMessage, task: Task, agent: Agent, exc: "NeedsHumanError") -> None:
        await _block_for_human_impl(db, message, task, agent, exc)

    @staticmethod
    async def _preferred_provider_id(db: AsyncSession, agent: Agent) -> str | None:
        """Phase 6: when the Arena connector is enabled (approved real Arena or the local mock),
        workers with their own browser profile default to ``arena_browser``; their configured
        API/local provider stays available as the router's alternative."""
        from app.browser.arena_connector import arena_gate, ensure_arena_browser_provider

        if agent.role == "worker" and agent.browser_profile_id and arena_gate()["enabled"]:
            provider = await ensure_arena_browser_provider(db)
            if provider.is_enabled:
                return provider.id
        return agent.provider_id

    async def _fail_unhandled(self, message_id: str, exc: Exception) -> None:
        async with self.session_factory() as db:
            await db.rollback()
            message = await db.get(AgentMessage, message_id)
            if not message or message.execution_status not in {"running", "paused"}:
                return
            task = await db.get(Task, message.task_id) if message.task_id else None
            agent = await db.get(Agent, message.recipient_id)
            if task is None or agent is None:
                message.execution_status = "failed"
                message.response_payload = {"error": f"{type(exc).__name__}: {_safe_excerpt(str(exc), 1_000)}"}
                await db.commit()
                return
            if agent.is_emergency_stopped or task.status in {"blocked", "cancelled"}:
                await self._pause_for_stop(db, message, task, agent, "Emergency stop took precedence over execution failure.")
                return
            if isinstance(exc, RepoAccessError):
                await _requeue_for_repo_access_impl(db, message, task, agent, exc)
                return
            if isinstance(exc, NeedsHumanError):
                await self._block_for_human(db, message, task, agent, exc)
                return
            error = f"{type(exc).__name__}: {_safe_excerpt(str(exc), 1_000)}"
            message.execution_status = "failed"
            message.response_payload = {**(message.response_payload or {}), "error": error}
            await task_scheduler.fail_and_retry_or_escalate(db, task, error)
            await self._store_outcome_memory(db, task, agent, "failed", error)
