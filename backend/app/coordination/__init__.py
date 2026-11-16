"""Redis-backed coordination (Phase 5): distributed locks, rate limits, WebSocket fan-out.

Optional: when ``REDIS_URL`` is unset every primitive falls back to an in-process
implementation with the same interface (correct for a single backend process).
"""

from app.coordination.redis_backend import (
    Coordinator,
    LockNotAcquired,
    get_coordinator,
    reset_coordinator,
)

__all__ = ["Coordinator", "LockNotAcquired", "get_coordinator", "reset_coordinator"]
