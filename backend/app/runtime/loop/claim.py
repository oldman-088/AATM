"""Atomic message claiming, unclaiming and broker-wake waiting for AgentLoop."""

from __future__ import annotations

import asyncio

from sqlalchemy import case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.base import utcnow
from app.models import (
    Agent,
    AgentMessage,
    Task,
)
from app.runtime.loop.common import (
    logger,
)


class ClaimMixin:
    """Mixin for AgentLoop; relies on attributes set in AgentLoop.__init__."""

    async def claim_one(self) -> str | None:
        """Claim one eligible running task message with one conditional UPDATE ... RETURNING."""
        message_candidate = aliased(AgentMessage)
        candidate_id = (
            select(message_candidate.id)
            .join(Task, Task.id == message_candidate.task_id)
            .join(Agent, Agent.id == message_candidate.recipient_id)
            .where(
                message_candidate.execution_status == "running",
                message_candidate.claimed_by.is_(None),
                Task.status == "in_progress",
                Task.assigned_agent_id == message_candidate.recipient_id,
                or_(Task.device_id.is_(None), Task.device_id == "device-local"),
                or_(Agent.device_id.is_(None), Agent.device_id == "device-local"),
                Agent.is_enabled.is_(True),
                Agent.is_emergency_stopped.is_(False),
            )
            .order_by(
                case(
                    (message_candidate.priority == "critical", 0),
                    (message_candidate.priority == "high", 1),
                    (message_candidate.priority == "medium", 2),
                    else_=3,
                ),
                message_candidate.created_at.asc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        async with self.session_factory() as db:
            statement = (
                update(AgentMessage)
                .where(
                    AgentMessage.id == candidate_id,
                    AgentMessage.execution_status == "running",
                    AgentMessage.claimed_by.is_(None),
                )
                .values(claimed_by=self.worker_id, claimed_at=utcnow())
                .returning(AgentMessage.id)
            )
            claimed_id = (await db.execute(statement)).scalar_one_or_none()
            if claimed_id is None:
                await db.rollback()
                return None
            await db.commit()
            return str(claimed_id)

    async def _wait_for_wake_or_poll(self) -> None:
        """Sleep until a broker wake message, stop, or the poll interval - whichever comes first.

        The wake is acknowledged only after the next claim cycle has run (its claim transaction
        committed), so a crash before claiming lets the broker redeliver. Duplicate wakes are
        harmless because claiming is an atomic UPDATE ... RETURNING on the AgentMessage row.
        """
        from app.queue.base import task_queue_name
        from app.queue.factory import get_wake_bus

        if self._pending_wake is not None:
            pending, self._pending_wake = self._pending_wake, None
            try:
                await pending.ack()
            except Exception:  # noqa: BLE001
                logger.warning("Wake ack failed; broker will redeliver (claim stays idempotent)")
        stop_waiter = asyncio.create_task(self._stop_event.wait())
        try:
            bus = get_wake_bus()
            wake_waiter = asyncio.create_task(bus.receive(task_queue_name("device-local"), self.poll_seconds))
        except Exception:  # noqa: BLE001 - broker misconfigured: plain polling
            wake_waiter = asyncio.create_task(asyncio.sleep(self.poll_seconds))
        try:
            done, _ = await asyncio.wait({stop_waiter, wake_waiter}, timeout=self.poll_seconds + 1, return_when=asyncio.FIRST_COMPLETED)
            if wake_waiter in done and not wake_waiter.cancelled() and wake_waiter.exception() is None:
                result = wake_waiter.result()
                if result is not None and hasattr(result, "ack"):
                    self.wakes_received += 1
                    self._pending_wake = result
            elif wake_waiter in done and wake_waiter.exception() is not None:
                logger.warning("Wake bus receive failed (%s); polling", type(wake_waiter.exception()).__name__)
                await asyncio.wait({stop_waiter}, timeout=self.poll_seconds)
        finally:
            for waiter in (stop_waiter, wake_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(stop_waiter, wake_waiter, return_exceptions=True)

    async def _unclaim(self, message_id: str) -> None:
        async with self.session_factory() as db:
            message = await db.get(AgentMessage, message_id)
            if message and message.execution_status == "running" and message.claimed_by == self.worker_id:
                message.claimed_by = None
                message.claimed_at = None
                await db.commit()

    async def _unclaim_in_session(self, db: AsyncSession, message: AgentMessage) -> None:
        message.claimed_by = None
        message.claimed_at = None
        await db.commit()
