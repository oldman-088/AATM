"""Message-broker wake bus (Phase 5): RabbitMQ (aio-pika) → Postgres LISTEN/NOTIFY → in-process.

The bus only *wakes* workers. The atomic ``UPDATE ... RETURNING`` claim in the database
stays the single source of truth, so a duplicated or redelivered wake message can never
cause double execution.
"""

from app.queue.base import WakeBus, WakeMessage
from app.queue.factory import get_wake_bus, reset_wake_bus, wake_bus_status

__all__ = ["WakeBus", "WakeMessage", "get_wake_bus", "reset_wake_bus", "wake_bus_status"]
