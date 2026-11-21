"""The scheduler gives no new tasks to Arena workers whose profile is disconnected from the bridge."""

from __future__ import annotations

import pytest

from app.browser import arena_connector
from app.models import Agent
from app.orchestration.scheduler import TaskScheduler


def _worker(agent_id: str, profile_id: str | None) -> Agent:
    return Agent(id=agent_id, name=agent_id, role="worker", browser_profile_id=profile_id)


@pytest.mark.asyncio
async def test_arena_off_keeps_dispatch_unchanged(monkeypatch):
    monkeypatch.setattr(arena_connector, "arena_gate", lambda: {"enabled": False})
    workers = [_worker("worker-1", "profile-worker-1")]
    assert await TaskScheduler._arena_disconnected_agent_ids(workers) == set()


@pytest.mark.asyncio
async def test_disconnected_worker_is_skipped_and_connected_worker_is_not(monkeypatch):
    monkeypatch.setattr(arena_connector, "arena_gate", lambda: {"enabled": True})

    async def fake_bridge(method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/v1/connector/profiles")
        return {"ok": True, "profiles": [
            {"profile_id": "profile-worker-1", "connected": True},
            {"profile_id": "profile-worker-2", "connected": False},
        ]}

    monkeypatch.setattr(arena_connector, "bridge_call", fake_bridge)
    workers = [_worker("worker-1", "profile-worker-1"), _worker("worker-2", "profile-worker-2"),
               _worker("worker-3", "profile-not-listed")]
    assert await TaskScheduler._arena_disconnected_agent_ids(workers) == {"worker-2", "worker-3"}


@pytest.mark.asyncio
async def test_reconnected_worker_is_eligible_again(monkeypatch):
    monkeypatch.setattr(arena_connector, "arena_gate", lambda: {"enabled": True})
    state = {"connected": False}

    async def fake_bridge(method, path, body=None, **kwargs):
        return {"ok": True, "profiles": [{"profile_id": "profile-worker-2", "connected": state["connected"]}]}

    monkeypatch.setattr(arena_connector, "bridge_call", fake_bridge)
    workers = [_worker("worker-2", "profile-worker-2")]
    assert await TaskScheduler._arena_disconnected_agent_ids(workers) == {"worker-2"}
    state["connected"] = True
    assert await TaskScheduler._arena_disconnected_agent_ids(workers) == set()


@pytest.mark.asyncio
async def test_unreachable_bridge_blocks_every_arena_worker(monkeypatch):
    monkeypatch.setattr(arena_connector, "arena_gate", lambda: {"enabled": True})

    async def down(method, path, body=None, **kwargs):
        return {"ok": False, "code": "bridge_unreachable"}

    monkeypatch.setattr(arena_connector, "bridge_call", down)
    workers = [_worker("worker-1", "profile-worker-1"), _worker("worker-2", None)]
    assert await TaskScheduler._arena_disconnected_agent_ids(workers) == {"worker-1"}
