"""P0 / E47: a slow or failed base-head lookup is retried before the task is handed to a human."""

from __future__ import annotations

import pytest

from app.orchestration import arena_runner
from app.orchestration.arena_runner import ArenaAgentRunner
from app.providers.adapters.arena_agent import ArenaAgentError

SHA = "a" * 40


@pytest.fixture
def fake_git(monkeypatch):
    """Replace `git ls-remote` with scripted results and skip the waits between attempts."""
    calls: list[list[str]] = []
    results: list[tuple[int, str, str]] = []

    async def fake_exec(cmd, cwd, timeout=30):
        calls.append(cmd)
        return results.pop(0)

    monkeypatch.setattr(arena_runner.git_workflow_manager, "_exec", fake_exec)
    monkeypatch.setattr(arena_runner, "BASE_HEAD_RETRY_DELAYS", (0.0, 0.0))
    return calls, results


async def test_timeouts_are_retried_and_the_third_lookup_wins(fake_git):
    calls, results = fake_git
    results += [(124, "", "Command timed out after 30s"), (124, "", "Command timed out after 30s"),
                (0, f"{SHA}\trefs/heads/main", "")]
    assert await ArenaAgentRunner._expected_base_head(None) == SHA
    assert len(calls) == 3


async def test_three_failures_raise_base_head_unavailable(fake_git):
    calls, results = fake_git
    results += [(124, "", "Command timed out after 30s")] * 3
    with pytest.raises(ArenaAgentError) as info:
        await ArenaAgentRunner._expected_base_head(None)
    assert info.value.code == "base_head_unavailable"
    assert "timed out" in info.value.message
    assert len(calls) == 3


async def test_missing_branch_is_not_retried(fake_git):
    calls, results = fake_git
    results += [(2, "", "")]
    with pytest.raises(ArenaAgentError) as info:
        await ArenaAgentRunner._expected_base_head(None)
    assert info.value.code == "base_head_unavailable"
    assert len(calls) == 1


async def test_first_success_does_not_wait(fake_git):
    calls, results = fake_git
    results += [(0, f"{SHA.upper()}\trefs/heads/main", "")]
    assert await ArenaAgentRunner._expected_base_head(None) == SHA
    assert len(calls) == 1
