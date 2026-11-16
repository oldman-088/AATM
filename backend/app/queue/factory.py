from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.queue.base import InProcessWakeBus, WakeBus

logger = logging.getLogger(__name__)
_bus: WakeBus | None = None


def _select_backend() -> tuple[str, str | None]:
    settings = get_settings()
    choice = (settings.QUEUE_BACKEND or "auto").strip().lower()
    db_url = settings.DATABASE_URL or ""
    if choice == "rabbitmq" or (choice == "auto" and settings.RABBITMQ_URL):
        return "rabbitmq", settings.RABBITMQ_URL
    if choice == "postgres" or (choice == "auto" and db_url.startswith(("postgresql", "postgres"))):
        return "postgres", db_url
    return "memory", None


def get_wake_bus() -> WakeBus:
    global _bus
    if _bus is None:
        backend, url = _select_backend()
        if backend == "rabbitmq" and url:
            from app.queue.rabbitmq import RabbitMQWakeBus

            _bus = RabbitMQWakeBus(url)
        elif backend == "postgres" and url:
            from app.queue.postgres import PostgresNotifyWakeBus

            _bus = PostgresNotifyWakeBus(url)
        else:
            _bus = InProcessWakeBus()
    return _bus


def reset_wake_bus(bus: WakeBus | None = None) -> None:
    global _bus
    _bus = bus


async def safe_publish_task(device_id: str | None, task_id: str, **extra: Any) -> bool:
    """Publish a wake; a broker outage never breaks dispatch (workers still poll the DB)."""
    try:
        await get_wake_bus().publish_task(device_id, task_id, **extra)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wake bus publish failed (%s); workers will pick the task up by polling", type(exc).__name__)
        return False


async def safe_publish(kind: str, payload: dict[str, Any]) -> bool:
    try:
        bus = get_wake_bus()
        if kind == "manager":
            await bus.publish_manager(payload)
        elif kind == "dead_letter":
            await bus.dead_letter(str(payload.get("task_id")), str(payload.get("reason", "")))
        else:
            await bus.publish_event(payload)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wake bus publish (%s) failed: %s", kind, type(exc).__name__)
        return False


def wake_bus_status() -> dict[str, Any]:
    backend, _ = _select_backend()
    status = {"configured_backend": backend}
    if _bus is not None:
        status.update(_bus.status())
    return status
