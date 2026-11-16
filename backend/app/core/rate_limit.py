"""Sliding-window rate limiting helpers for authentication endpoints."""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """In-memory per-key sliding-window rate limiter (Redis-replaceable)."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, dq: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while dq and dq[0] <= cutoff:
            dq.popleft()

    def check_allowed(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            dq = self._failures.get(key)
            if not dq:
                return True, 0
            self._prune(dq, now)
            if len(dq) >= self.max_attempts:
                retry_after = max(1, int(dq[0] + self.window_seconds - now))
                return False, retry_after
            return True, 0

    def record_failure(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            dq = self._failures.setdefault(key, deque())
            self._prune(dq, now)
            dq.append(now)
            return len(dq)

    def reset_key(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


login_rate_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=300)


class SharedLoginRateLimiter:
    """Async facade: Redis sorted-set sliding window when REDIS_URL is set (shared by every
    backend process), otherwise the in-memory limiter above. Same limits either way."""

    def __init__(self, local: SlidingWindowRateLimiter) -> None:
        self.local = local

    @staticmethod
    def _redis():
        from app.coordination import get_coordinator
        from app.coordination.redis_backend import RedisCoordinator

        coordinator = get_coordinator()
        return coordinator._redis if isinstance(coordinator, RedisCoordinator) else None

    async def check_allowed(self, key: str) -> tuple[bool, int]:
        redis = self._redis()
        if redis is None:
            return self.local.check_allowed(key)
        now = time.time()
        rkey = f"aatm:login_fail:{key}"
        await redis.zremrangebyscore(rkey, 0, now - self.local.window_seconds)
        count = await redis.zcard(rkey)
        if count >= self.local.max_attempts:
            oldest = await redis.zrange(rkey, 0, 0, withscores=True)
            first = oldest[0][1] if oldest else now
            return False, max(1, int(first + self.local.window_seconds - now))
        return True, 0

    async def record_failure(self, key: str) -> int:
        redis = self._redis()
        if redis is None:
            return self.local.record_failure(key)
        now = time.time()
        rkey = f"aatm:login_fail:{key}"
        async with redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(rkey, 0, now - self.local.window_seconds)
            pipe.zadd(rkey, {f"{now:.6f}:{threading.get_ident()}": now})
            pipe.zcard(rkey)
            pipe.expire(rkey, int(self.local.window_seconds) + 1)
            _, _, count, _ = await pipe.execute()
        return int(count)

    async def reset_key(self, key: str) -> None:
        redis = self._redis()
        if redis is None:
            self.local.reset_key(key)
            return
        await redis.delete(f"aatm:login_fail:{key}")


shared_login_rate_limiter = SharedLoginRateLimiter(login_rate_limiter)
