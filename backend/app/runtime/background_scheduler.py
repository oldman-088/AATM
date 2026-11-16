"""Periodic background orchestration and recovery scheduler."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.orchestration.scheduler import TaskScheduler, task_scheduler
from app.qa.engine import QAAndIntelligenceEngine, qa_engine

logger = logging.getLogger(__name__)


class BackgroundScheduler:
    """Run orchestration and recovery sweeps on independent, non-overlapping loops."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        scheduler: TaskScheduler | None = None,
        self_healer: QAAndIntelligenceEngine | None = None,
        interval_seconds: float | None = None,
        self_healing_interval_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self.session_factory = session_factory
        self.scheduler = scheduler or task_scheduler
        self.self_healer = self_healer or qa_engine
        self.interval_seconds = float(
            interval_seconds if interval_seconds is not None else settings.SCHEDULER_INTERVAL_SECONDS
        )
        self.self_healing_interval_seconds = float(
            self_healing_interval_seconds
            if self_healing_interval_seconds is not None
            else settings.SELF_HEALING_INTERVAL_SECONDS
        )
        if not math.isfinite(self.interval_seconds) or self.interval_seconds <= 0:
            raise ValueError("Scheduler interval must be a finite positive number.")
        if not math.isfinite(self.self_healing_interval_seconds) or self.self_healing_interval_seconds <= 0:
            raise ValueError("Self-healing interval must be a finite positive number.")

    async def run_orchestration_forever(self) -> None:
        """Run a cycle immediately, then at the configured interval."""
        await self._run_periodically(
            self.interval_seconds,
            self._run_orchestration_cycle,
            run_immediately=True,
        )

    async def run_self_healing_forever(self) -> None:
        """Wait for the configured interval before the first recovery sweep."""
        await self._run_periodically(
            self.self_healing_interval_seconds,
            self._run_self_healing,
            run_immediately=False,
        )

    async def _run_periodically(
        self,
        interval_seconds: float,
        operation: Callable[[], Awaitable[None]],
        *,
        run_immediately: bool,
    ) -> None:
        loop = asyncio.get_running_loop()
        try:
            if not run_immediately:
                await asyncio.sleep(interval_seconds)
            while True:
                started_at = loop.time()
                await operation()
                delay = max(0.0, interval_seconds - (loop.time() - started_at))
                if delay:
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    async def _run_orchestration_cycle(self) -> None:
        try:
            async with self.session_factory() as db:
                result = await self.scheduler.run_orchestration_cycle(db)
            logger.debug("Background orchestration cycle complete: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background orchestration cycle failed; the next interval will retry")

    async def _run_self_healing(self) -> None:
        try:
            async with self.session_factory() as db:
                result = await self.self_healer.run_self_healing(db)
            logger.info("Background self-healing sweep complete: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background self-healing sweep failed; the next interval will retry")
