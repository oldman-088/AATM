import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class EventBus:
    """In-process WebSocket & async event broadcaster for live AATM monitoring."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._history: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, *, accept: bool = True) -> None:
        if accept:
            await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "event_id": str(uuid.uuid4()),
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        await self._deliver_local(event)
        # Multi-process fan-out via Redis pub/sub when REDIS_URL is set (no-op in-process otherwise).
        try:
            from app.coordination import get_coordinator

            await get_coordinator().publish_event(event)
        except Exception as exc:  # noqa: BLE001 - live updates must never break the emitting request
            logger.debug("Event fan-out skipped: %s", type(exc).__name__)
        return event

    async def deliver_remote(self, event: dict[str, Any]) -> None:
        """Deliver an event published by another backend process (from the Redis subscriber)."""
        if isinstance(event, dict) and event.get("type"):
            await self._deliver_local(event)

    async def _deliver_local(self, event: dict[str, Any]) -> None:
        async with self._lock:
            self._history.append(event)
            if len(self._history) > 200:
                self._history = self._history[-200:]
            conns = list(self._connections)

        dead: list[WebSocket] = []
        text = json.dumps(event, default=str)
        for ws in conns:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._history[-limit:])


event_bus = EventBus()
