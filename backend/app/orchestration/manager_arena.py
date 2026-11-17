"""manager-0 on Arena Agent mode (R7): the manager's own Arena session, driven like the workers'."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def manager_activity(db: AsyncSession) -> dict[str, Any] | None:
    """What the manager is doing right now (filled in by the manager runtime)."""
    return None
