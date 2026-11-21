"""Regression: a command sent right after "New session" must reach the NEW page, not the one that is unloading."""

from __future__ import annotations

from typing import Any

from app.providers.adapters.arena_agent import ArenaAgentAdapter


def _adapter(states: list[dict[str, Any]], log: list[str], *, navigating: bool = True) -> ArenaAgentAdapter:
    adapter = ArenaAgentAdapter()
    adapter.navigation_settle_seconds = 0
    adapter.reconnect_poll_seconds = 0
    adapter.reconnect_timeout_seconds = 0.5
    queue = list(states)

    async def state(profile_id: str) -> dict[str, Any]:
        log.append("state")
        return dict(queue.pop(0) if len(queue) > 1 else queue[0])

    async def command(profile_id: str, name: str, **kwargs: Any) -> dict[str, Any]:
        log.append(f"command:{name}")
        return {"started": True, "navigating": navigating}

    adapter.state = state  # type: ignore[method-assign]
    adapter.command = command  # type: ignore[method-assign]
    return adapter


def _page(total: int, agent_state: str = "ready") -> dict[str, Any]:
    return {"connected": True, "status": "ready", "agent_state": agent_state, "connections_total": total}


async def test_new_session_does_not_trust_the_old_pages_ready_state():
    # Before the click: connection 5. The old page keeps saying "ready" for three polls. Only the new page
    # (connection 6) may release the caller; the old page's "ready" is what used to lose the next command.
    log: list[str] = []
    adapter = _adapter([_page(5), _page(5), _page(5), _page(5), _page(6)], log)
    await adapter.new_session("p")
    assert log[0] == "state" and log[1] == "command:new_session"
    assert log.count("state") >= 5, "it must keep polling until the connection counter rose"


async def test_new_session_stops_waiting_when_the_window_never_reconnects():
    log: list[str] = []
    adapter = _adapter([_page(5)], log)  # navigating but the counter never moves
    result = await adapter.new_session("p")  # falls back to the state check after the short timeout
    assert result["started"] is True


async def test_new_session_without_navigation_or_counter_behaves_as_before():
    log: list[str] = []
    adapter = _adapter([{"connected": True, "agent_state": "ready"}], log, navigating=False)
    await adapter.new_session("p")
    assert log.count("command:new_session") == 1
    assert log.count("state") <= 3
