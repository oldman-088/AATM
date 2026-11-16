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
