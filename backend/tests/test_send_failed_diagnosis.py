"""E79: a "send_failed" is looked at on the page: sent after all, retried once, or explained in the error."""

from __future__ import annotations

from typing import Any

import pytest

from app.providers.adapters.arena_agent import ArenaAgentAdapter, ArenaAgentError


def _page(state: str = "ready", *, visibility: str = "visible", stop: bool = False, human: bool = False,
          send_disabled: bool = True) -> dict[str, Any]:
    return {"state": state, "composer": {"found": True, "disabled": False, "has_text": False},
            "send": {"found": True, "disabled": send_disabled}, "stop": {"found": stop},
            "human_check": {"found": human, "reason": "human check element on page" if human else None},
            "offline": {"found": False}, "page": {"visibility": visibility, "path": "/agent"}}


def _adapter(submits: list[Any], page: dict[str, Any], log: list[str]) -> ArenaAgentAdapter:
    adapter = ArenaAgentAdapter()
    adapter.send_retry_wait_seconds = 0
    queue = list(submits)

    async def command(profile_id: str, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        log.append(name)
        if name == "selector_test":
            return page
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    adapter.command = command  # type: ignore[method-assign]
    return adapter


def _failed() -> ArenaAgentError:
    return ArenaAgentError("send_failed", "text was typed but the page did not send it")


async def test_a_running_agent_after_send_failed_means_the_message_went_out():
    log: list[str] = []
    adapter = _adapter([_failed()], _page("generating", stop=True), log)
    result = await adapter.submit("p", "hello")
    assert result["sent"] and result["how"] == "seen_running_after_send_failed"
    assert log == ["submit", "selector_test"]  # never sent twice


async def test_a_quiet_visible_page_gets_one_more_try():
    log: list[str] = []
    adapter = _adapter([_failed(), {"sent": True, "how": "send_button"}], _page(), log)
    assert (await adapter.submit("p", "hello"))["sent"]
    assert log == ["submit", "selector_test", "submit"]


async def test_a_second_failure_carries_what_the_page_showed():
    log: list[str] = []
    adapter = _adapter([_failed(), _failed()], _page(), log)
    with pytest.raises(ArenaAgentError) as raised:
        await adapter.submit("p", "hello")
    assert raised.value.code == "send_failed"
    assert "state=ready" in raised.value.message and "send_button=disabled" in raised.value.message
    assert log.count("submit") == 2


async def test_a_hidden_page_or_a_human_check_is_not_retried():
    for page in (_page(visibility="hidden"), _page(human=True)):
        log: list[str] = []
        adapter = _adapter([_failed()], page, log)
        with pytest.raises(ArenaAgentError) as raised:
            await adapter.submit("p", "hello")
        assert log.count("submit") == 1
        assert "visibility=hidden" in raised.value.message or "human_check=" in raised.value.message


async def test_other_errors_pass_through_untouched():
    log: list[str] = []
    adapter = _adapter([ArenaAgentError("busy", "the agent is still generating")], _page(), log)
    with pytest.raises(ArenaAgentError) as raised:
        await adapter.submit("p", "hello")
    assert raised.value.code == "busy" and log == ["submit"]
