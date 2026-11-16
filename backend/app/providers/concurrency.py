"""Per-provider in-flight limits (Phase 5). ``PROVIDER_CONCURRENCY_LIMITS="id=4,other=8"``; unlisted = unlimited."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from app.core.config import get_settings

_SEMAPHORES: dict[str, tuple[int, asyncio.Semaphore]] = {}
_IN_FLIGHT: dict[str, int] = {}


def parse_limits(raw: str | None) -> dict[str, int]:
    limits: dict[str, int] = {}
    for part in (raw or "").split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        try:
            n = int(value.strip())
        except ValueError:
            continue
        if key.strip() and n >= 1:
            limits[key.strip()] = n
    return limits


def provider_limit(provider_id: str) -> int | None:
    return parse_limits(get_settings().PROVIDER_CONCURRENCY_LIMITS).get(provider_id)


@contextlib.asynccontextmanager
async def provider_slot(provider_id: str) -> AsyncIterator[None]:
    limit = provider_limit(provider_id)
    if limit is None:
        _IN_FLIGHT[provider_id] = _IN_FLIGHT.get(provider_id, 0) + 1
        try:
            yield
        finally:
            _IN_FLIGHT[provider_id] -= 1
        return
    held = _SEMAPHORES.get(provider_id)
    if held is None or held[0] != limit:
        held = (limit, asyncio.Semaphore(limit))
        _SEMAPHORES[provider_id] = held
    async with held[1]:
        _IN_FLIGHT[provider_id] = _IN_FLIGHT.get(provider_id, 0) + 1
        try:
            yield
        finally:
            _IN_FLIGHT[provider_id] -= 1


def concurrency_status() -> dict[str, dict[str, int | None]]:
    limits = parse_limits(get_settings().PROVIDER_CONCURRENCY_LIMITS)
    keys = set(limits) | {k for k, v in _IN_FLIGHT.items() if v}
    return {k: {"limit": limits.get(k), "in_flight": _IN_FLIGHT.get(k, 0)} for k in sorted(keys)}
