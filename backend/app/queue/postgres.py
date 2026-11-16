"""Postgres LISTEN/NOTIFY wake bus — fallback for small installs without RabbitMQ.

NOTIFY is not durable: a wake sent while no worker listens is lost. That is safe because
workers also poll the database on ``AGENT_LOOP_POLL_SECONDS`` and the DB claim is the
source of truth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.queue.base import WakeBus, WakeMessage

logger = logging.getLogger(__name__)
CHANNEL = "aatm_wake"


def _libpq_url(url: str) -> str:
    return re.sub(r"^postgresql\+(asyncpg|psycopg|psycopg2)://", "postgresql://", url)


class PostgresNotifyWakeBus(WakeBus):
    backend = "postgres"

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = _libpq_url(url)
        self._listen_conn = None
        self._send_conn = None
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def _q(self, name: str) -> asyncio.Queue[dict[str, Any]]:
        if name not in self._queues:
            self._queues[name] = asyncio.Queue(maxsize=10_000)
        return self._queues[name]

    async def start(self) -> None:
        import psycopg

        async with self._lock:
            if self._reader is not None and not self._reader.done():
                return
            self._listen_conn = await psycopg.AsyncConnection.connect(self.url, autocommit=True)
            self._send_conn = await psycopg.AsyncConnection.connect(self.url, autocommit=True)
            await self._listen_conn.execute(f"LISTEN {CHANNEL}")
            self._reader = asyncio.create_task(self._read_loop(), name="aatm-pg-notify-reader")

    async def _read_loop(self) -> None:
        try:
            async for notify in self._listen_conn.notifies():
                try:
                    data = json.loads(notify.payload)
                    queue, payload = data["q"], data["p"]
                except (KeyError, ValueError, TypeError):
                    continue
                q = self._q(queue)
                if q.full():
                    q.get_nowait()
                q.put_nowait(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Postgres LISTEN loop ended; workers fall back to DB polling")

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        for conn in (self._listen_conn, self._send_conn):
            if conn is not None:
                await conn.close()
        self._reader = self._listen_conn = self._send_conn = None

    async def publish(self, queue: str, payload: dict[str, Any]) -> None:
        await self.start()
        body = json.dumps({"q": queue, "p": payload}, default=str)
        if len(body) > 7_900:  # NOTIFY payload limit is 8000 bytes
            body = json.dumps({"q": queue, "p": {"task_id": payload.get("task_id"), "truncated": True}})
        await self._send_conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, body))
        self.published += 1

    async def receive(self, queue: str, timeout: float) -> WakeMessage | None:
        await self.start()
        try:
            payload = await asyncio.wait_for(self._q(queue).get(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        return WakeMessage(queue=queue, payload=payload)
