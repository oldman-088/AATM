"""E78: the bridge remembers that a page is hidden, so the watchdog can bring its window back."""

from __future__ import annotations

import asyncio

import pytest
from bridge.connector import ConnectorError, ConnectorHub, ProfileState


def _state() -> ProfileState:
    return ProfileState(profile_id="profile-worker-5", agent_id=None, token_hash="0" * 64,
                        allowed_origins=["https://arena.ai"], real_enabled=True, mode="agent")


@pytest.mark.asyncio
async def test_tab_events_set_and_clear_the_hidden_flag():
    hub, state = ConnectorHub(), _state()
    assert state.summary()["page_hidden"] is False
    await hub._on_message(state, {"type": "event", "kind": "tab_hidden"})
    summary = state.summary()
    assert summary["page_hidden"] is True and summary["page_hidden_seconds"] is not None
    await hub._on_message(state, {"type": "event", "kind": "tab_visible"})
    assert state.summary()["page_hidden"] is False


@pytest.mark.asyncio
async def test_a_command_refused_with_page_hidden_marks_the_page_hidden():
    hub, state = ConnectorHub(), _state()
    future = asyncio.get_running_loop().create_future()
    state.pending["r1"] = future
    await hub._on_message(state, {"type": "result", "request_id": "r1", "ok": False, "code": "page_hidden",
                                  "message": "the Arena window is minimised"})
    with pytest.raises(ConnectorError):
        future.result()
    assert state.summary()["page_hidden"] is True
