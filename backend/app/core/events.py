import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket


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
        return event

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._history[-limit:])


event_bus = EventBus()
