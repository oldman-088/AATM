from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "aatm:events"
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class LockNotAcquired(RuntimeError):
    pass


class Coordinator:
    """Common interface; ``InProcessCoordinator`` and ``RedisCoordinator`` implement it."""

    backend = "abstract"

    async def acquire_lock(self, key: str, ttl_seconds: float) -> str | None:
        raise NotImplementedError

    async def release_lock(self, key: str, token: str) -> bool:
        raise NotImplementedError

    @contextlib.asynccontextmanager
    async def lock(self, key: str, ttl_seconds: float = 900.0, *, wait_seconds: float = 0.0) -> AsyncIterator[str]:
        deadline = time.monotonic() + wait_seconds
        while True:
            token = await self.acquire_lock(key, ttl_seconds)
            if token is not None:
                break
            if time.monotonic() >= deadline:
                raise LockNotAcquired(key)
            await asyncio.sleep(0.05)
        try:
            yield token
        finally:
            await self.release_lock(key, token)

    async def hit(self, key: str, *, limit: int, window_seconds: float) -> tuple[bool, int]:
        """Fixed-window counter. Returns (allowed, count_in_window)."""
        raise NotImplementedError

    async def publish_event(self, event: dict[str, Any]) -> None:
        raise NotImplementedError

    async def subscribe_events(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> asyncio.Task | None:
        return None

    async def close(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {"backend": self.backend}


class InProcessCoordinator(Coordinator):
    backend = "memory"

    def __init__(self) -> None:
        self._locks: dict[str, tuple[str, float]] = {}
        self._counters: dict[str, tuple[int, float]] = {}
        self._mutex = asyncio.Lock()

    async def acquire_lock(self, key: str, ttl_seconds: float) -> str | None:
        now = time.monotonic()
        async with self._mutex:
            held = self._locks.get(key)
            if held and held[1] > now:
                return None
            token = secrets.token_hex(16)
            self._locks[key] = (token, now + ttl_seconds)
            return token

    async def release_lock(self, key: str, token: str) -> bool:
        async with self._mutex:
            held = self._locks.get(key)
            if held and held[0] == token:
                del self._locks[key]
                return True
            return False

    async def hit(self, key: str, *, limit: int, window_seconds: float) -> tuple[bool, int]:
        now = time.monotonic()
        async with self._mutex:
            count, reset_at = self._counters.get(key, (0, now + window_seconds))
            if now >= reset_at:
                count, reset_at = 0, now + window_seconds
            if count >= limit:
                self._counters[key] = (count, reset_at)
                return False, count
            self._counters[key] = (count + 1, reset_at)
            return True, count + 1

    async def publish_event(self, event: dict[str, Any]) -> None:
        return None  # single process: the local EventBus already delivered it


class RedisCoordinator(Coordinator):
    backend = "redis"

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis_asyncio

        self.url = url
        self._redis = redis_asyncio.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        self.instance_id = secrets.token_hex(8)

    async def acquire_lock(self, key: str, ttl_seconds: float) -> str | None:
        token = secrets.token_hex(16)
        ok = await self._redis.set(f"aatm:lock:{key}", token, nx=True, px=int(ttl_seconds * 1000))
        return token if ok else None

    async def release_lock(self, key: str, token: str) -> bool:
        return bool(await self._redis.eval(_RELEASE_LUA, 1, f"aatm:lock:{key}", token))

    async def hit(self, key: str, *, limit: int, window_seconds: float) -> tuple[bool, int]:
        rkey = f"aatm:rate:{key}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(rkey)
            pipe.pexpire(rkey, int(window_seconds * 1000), nx=True)
            count, _ = await pipe.execute()
        count = int(count)
        if count > limit:
            return False, limit
        return True, count

    async def publish_event(self, event: dict[str, Any]) -> None:
        await self._redis.publish(EVENTS_CHANNEL, json.dumps({"origin": self.instance_id, "event": event}, default=str))

    async def subscribe_events(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> asyncio.Task:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(EVENTS_CHANNEL)

        async def _reader() -> None:
            try:
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        data = json.loads(message["data"])
                    except (TypeError, ValueError):
                        continue
                    if data.get("origin") == self.instance_id:
                        continue  # already delivered locally
                    await handler(data.get("event") or {})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Redis event fan-out subscriber stopped")
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()

        return asyncio.create_task(_reader(), name="aatm-redis-fanout")

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def close(self) -> None:
        await self._redis.aclose()


_coordinator: Coordinator | None = None


def get_coordinator() -> Coordinator:
    global _coordinator
    if _coordinator is None:
        url = (get_settings().REDIS_URL or "").strip()
        _coordinator = RedisCoordinator(url) if url else InProcessCoordinator()
    return _coordinator


def reset_coordinator(coordinator: Coordinator | None = None) -> None:
    global _coordinator
    _coordinator = coordinator
