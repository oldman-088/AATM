from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

TASKS_QUEUE_PREFIX = "aatm.tasks."
MANAGER_QUEUE = "aatm.manager"
EVENTS_QUEUE = "aatm.events"
DEAD_LETTER_QUEUE = "aatm.dead_letter"


def task_queue_name(device_id: str | None) -> str:
    return f"{TASKS_QUEUE_PREFIX}{device_id or 'device-local'}"


@dataclass
class WakeMessage:
    """One wake-up. ``ack`` must be awaited after the woken claim transaction committed."""

    queue: str
    payload: dict[str, Any]
    _ack: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    _nack: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    async def ack(self) -> None:
        if self._ack is not None:
            fn, self._ack, self._nack = self._ack, None, None
            await fn()

    async def reject_to_dead_letter(self) -> None:
        if self._nack is not None:
            fn, self._ack, self._nack = self._nack, None, None
            await fn()


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")


class WakeBus(ABC):
    backend: str = "abstract"

    def __init__(self) -> None:
        self.published: int = 0
        self.dead_lettered: int = 0

    async def start(self) -> None:  # pragma: no cover - default no-op
        return None

    async def stop(self) -> None:  # pragma: no cover - default no-op
        return None

    @abstractmethod
    async def publish(self, queue: str, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    async def receive(self, queue: str, timeout: float) -> WakeMessage | None:
        """Wait up to ``timeout`` seconds for one wake message on ``queue``."""

    async def publish_task(self, device_id: str | None, task_id: str, **extra: Any) -> None:
        await self.publish(task_queue_name(device_id), {"task_id": task_id, **extra})

    async def publish_manager(self, payload: dict[str, Any]) -> None:
        await self.publish(MANAGER_QUEUE, payload)

    async def publish_event(self, payload: dict[str, Any]) -> None:
        await self.publish(EVENTS_QUEUE, payload)

    async def dead_letter(self, task_id: str, reason: str) -> None:
        self.dead_lettered += 1
        await self.publish(DEAD_LETTER_QUEUE, {"task_id": task_id, "reason": reason[:500]})

    def status(self) -> dict[str, Any]:
        return {"backend": self.backend, "published": self.published, "dead_lettered": self.dead_lettered}


class InProcessWakeBus(WakeBus):
    """Single-process fallback (dev / tests). Same semantics, no durability."""

    backend = "memory"

    def __init__(self) -> None:
        super().__init__()
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}

    def _q(self, name: str) -> asyncio.Queue[dict[str, Any]]:
        if name not in self._queues:
            self._queues[name] = asyncio.Queue(maxsize=10_000)
        return self._queues[name]

    async def publish(self, queue: str, payload: dict[str, Any]) -> None:
        q = self._q(queue)
        if q.full():  # wake signals are idempotent; dropping the oldest is safe
            q.get_nowait()
        q.put_nowait(payload)
        self.published += 1

    async def receive(self, queue: str, timeout: float) -> WakeMessage | None:
        try:
            payload = await asyncio.wait_for(self._q(queue).get(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        return WakeMessage(queue=queue, payload=payload)

    def depth(self, queue: str) -> int:
        return self._q(queue).qsize()
