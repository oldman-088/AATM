"""P0 / E39: a quiet, unfinished worker is told "continue" before it is handed to a human."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.orchestration import arena_runner
from app.orchestration.arena_runner import STALL_CONTINUES, ArenaAgentRunner
from app.providers.adapters.arena_agent import TOOL_LIMIT_CONTINUE

TASK = "6f0d1c2e-3b4a-4c5d-8e9f-0a1b2c3d4e5f"
_TASK = SimpleNamespace(id=TASK, title="t", status="in_progress", updated_at=None)


class _Db:
    async def commit(self) -> None:
        return None

    async def get(self, model, _key):
        return _TASK if model is arena_runner.Task else None


class _Adapter:
    def __init__(self, page: str) -> None:
        self.page = page

    async def state(self, _profile):
        return {"agent_state": self.page}


def _runner(monkeypatch, page: str, outcome: list):
    runner = ArenaAgentRunner.__new__(ArenaAgentRunner)
    runner.adapter, runner.stall_minutes, runner._quiet_misses = _Adapter(page), 20, {}

    async def _project(*_args):
        return None

    async def _true(*_args, **_kwargs):
        return True

    async def _false(*_args, **_kwargs):
        return False

    async def _none(*_args, **_kwargs):
        return None

    async def _follow_up(_db, session, kind, text):
        outcome.append((kind, text))
        session.events.append({"kind": kind})
        return True

    async def _block(_db, _session, _task, reason):
        outcome.append(("human", reason))

    runner._project, runner._record_chat_path, runner._on_task_chat = _project, _none, _true
    runner._reports_github_blocked, runner._page_progressed, runner._note_page_mark = _false, _false, _none
    runner._follow_up, runner._block_needs_human = _follow_up, _block
    monkeypatch.setattr(arena_runner, "apply_bridge_state", lambda *_args: False)
    monkeypatch.setattr(arena_runner, "append_event", lambda s, kind, *_a, **_k: s.events.append({"kind": kind}))
    return runner


def _session(page: str, *, pr_number=None, continues: int = 0):
    return SimpleNamespace(
        id="s", task_id=TASK, active_task_id=TASK, profile_id="p", page_state=page, pr_number=pr_number,
        submitted_at=datetime(2020, 1, 1), last_state_change_at=datetime(2020, 1, 1),
        events=[{"kind": "stall_continue"} for _ in range(continues)], recovery_count=0, phase="working",
        needs_human_reason=None, keep_working_count=0, updated_at=None,
    )


@pytest.mark.parametrize("page", ["idle", "ready", "tool_call_limit"])
async def test_quiet_unfinished_worker_gets_continue_instead_of_needs_human(monkeypatch, page):
    outcome: list = []
    runner = _runner(monkeypatch, page, outcome)
    result = await runner._tick_session(_Db(), _session(page))
    assert outcome == [("stall_continue", TOOL_LIMIT_CONTINUE)]
    assert result["stall_continue"] == 1


async def test_second_continue_is_counted(monkeypatch):
    outcome: list = []
    runner = _runner(monkeypatch, "idle", outcome)
    result = await runner._tick_session(_Db(), _session("idle", continues=1))
    assert outcome == [("stall_continue", TOOL_LIMIT_CONTINUE)]
    assert result["stall_continue"] == 2


async def test_after_the_allowed_continues_the_owner_is_asked(monkeypatch):
    outcome: list = []
    runner = _runner(monkeypatch, "idle", outcome)
    result = await runner._tick_session(_Db(), _session("idle", continues=STALL_CONTINUES))
    assert [kind for kind, _ in outcome] == ["human"]
    assert result["stalled"] is True


async def test_generating_without_progress_is_not_sent_continue(monkeypatch):
    outcome: list = []
    runner = _runner(monkeypatch, "generating", outcome)
    await runner._tick_session(_Db(), _session("generating"))
    assert [kind for kind, _ in outcome] == ["human"]


async def test_failed_continue_falls_back_to_the_owner(monkeypatch):
    outcome: list = []
    runner = _runner(monkeypatch, "idle", outcome)

    async def _failed_follow_up(_db, _session, kind, _text):
        outcome.append((f"{kind}_failed", ""))
        return False

    runner._follow_up = _failed_follow_up
    await runner._tick_session(_Db(), _session("idle"))
    assert [kind for kind, _ in outcome] == ["stall_continue_failed", "human"]
