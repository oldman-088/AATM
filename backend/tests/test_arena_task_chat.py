"""After a restart the worker window opens on Arena's start page, not on the task's chat.

AATM records the chat it sent the task to, brings a quiet window back to it, never navigates a
busy window, and asks the owner (instead of retrying every minute) when a follow-up cannot be sent.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures

from app.db.base import utcnow
from app.models import Agent, ArenaSession, Project, Task
from app.orchestration import arena_runner as runner_module
from app.orchestration.arena_runner import (
    CHAT_REOPEN_ATTEMPTS,
    FOLLOW_UP_FAILURES_BEFORE_HUMAN,
    ArenaAgentRunner,
    chat_path_matches_branch,
    task_chat_path,
)
from app.providers.adapters.arena_agent import ArenaAgentAdapter, ArenaAgentError

CHAT = "/agent/01a1ef92-b369-7e9d-8a27-c554f5fddb3d"


class Page(ArenaAgentAdapter):
    """A worker window whose URL and state the test controls; records every command."""

    def __init__(self, path: str = "/agent", state: str = "ready") -> None:
        super().__init__()
        self.path, self.page_state = path, state
        self.commands: list[tuple[str, str | None]] = []
        self.submit_error: tuple[str, str] | None = None

    async def state(self, profile_id: str) -> dict[str, Any]:
        return {"ok": True, "connected": True, "mode": "agent", "status": "ready", "agent_state": self.page_state}

    async def command(self, profile_id: str, command: str, text: str | None = None, *, timeout: float | None = None,
                      repo: str | None = None, branch: str | None = None, url: str | None = None) -> dict[str, Any]:
        self.commands.append((command, url))
        if command == "selector_test":
            return {"page": {"origin": "https://arena.ai", "path": self.path}, "state": self.page_state}
        if command == "new_session":
            return {"started": True, "navigating": True}
        if command == "submit":
            if self.submit_error:
                raise ArenaAgentError(*self.submit_error)
            return {"sent": True}
        return {}

    def sent(self, command: str) -> list[str | None]:
        return [url for name, url in self.commands if name == command]


async def _setup(env, events: list[dict[str, Any]] | None = None) -> tuple[str, str]:  # noqa: F811
    async with env() as db:
        project = Project(name="Chat", slug="chat", repo_path="/nonexistent", repo_url="https://github.com/o/r",
                          default_branch="main")
        db.add(project)
        await db.flush()
        agent = await db.get(Agent, "worker-1")
        task = Task(project_id=project.id, title="Fix it", description="d", status="in_progress",
                    assigned_agent_id=agent.id, execution_mode="manual")
        db.add(task)
        await db.flush()
        session = ArenaSession(agent_id=agent.id, profile_id=agent.browser_profile_id, task_id=task.id, active_task_id=task.id,
                               phase="working", page_state="ready", submitted_at=utcnow(), last_state_change_at=utcnow(),
                               events=events if events is not None else [{"kind": "chat_path", "detail": CHAT}])
        db.add(session)
        agent.status, agent.current_task_id = "working", task.id
        await db.commit()
        return task.id, session.id


def test_chat_path_rules():
    session = ArenaSession(events=[{"kind": "chat_url", "detail": CHAT}])
    assert task_chat_path(session) == CHAT  # the older event name still counts
    assert task_chat_path(ArenaSession(events=[{"kind": "chat_path", "detail": "/agent"}])) is None  # start page, no chat id
    assert task_chat_path(ArenaSession(events=[{"kind": "chat_path", "detail": "https://evil.example/agent/0123456789"}])) is None
    assert task_chat_path(ArenaSession(events=[])) is None
    assert chat_path_matches_branch(CHAT, "arena/f5fddb3d-aatm")
    assert not chat_path_matches_branch(CHAT, "arena/ff0f70cb-aatm")  # another chat's branch
    assert chat_path_matches_branch(CHAT, None)


async def test_quiet_window_on_the_start_page_is_sent_back_to_the_task_chat(env):  # noqa: F811
    _task_id, session_id = await _setup(env)
    page = Page(path="/agent")
    runner = ArenaAgentRunner(page)
    async with env() as db:
        result = await runner.tick(db)
    assert result[session_id] == {"phase": "working", "chat": "returning"}
    assert page.sent("new_session") == [f"https://arena.ai{CHAT}"]
    assert page.sent("submit") == [] and page.sent("read_last_reply") == []  # nothing reached the wrong chat
    async with env() as db:
        session = await db.get(ArenaSession, session_id)
        assert session.events[-1]["kind"] == "chat_reopened" and session.phase == "working"

    page.path = CHAT  # the chat finished loading: work continues and the check is not repeated every tick
    async with env() as db:
        session = await db.get(ArenaSession, session_id)
        task = await db.get(Task, session.active_task_id)
        assert await runner._on_task_chat(db, session, task, changed=True) is True
        assert await runner._on_task_chat(db, session, task, changed=False) is True
    assert page.sent("selector_test").count(None) == 2


async def test_busy_window_in_another_chat_is_never_navigated(env):  # noqa: F811
    _task_id, session_id = await _setup(env)
    page = Page(path="/agent/99999999-other-chat", state="generating")
    runner = ArenaAgentRunner(page)
    async with env() as db:
        session = await db.get(ArenaSession, session_id)
        task = await db.get(Task, session.active_task_id)
        assert await runner._on_task_chat(db, session, task, changed=True) is False
    assert page.sent("new_session") == []
    async with env() as db:
        session, task = await db.get(ArenaSession, session_id), await db.get(Task, _task_id)
        assert session.phase == "needs_human" and "busy in another chat" in session.needs_human_reason
        assert task.status == "blocked"


async def test_window_that_will_not_stay_on_the_chat_goes_to_the_owner(env):  # noqa: F811
    task_id, session_id = await _setup(env)
    page = Page(path="/agent")
    runner = ArenaAgentRunner(page)
    for _ in range(CHAT_REOPEN_ATTEMPTS + 1):
        async with env() as db:
            session = await db.get(ArenaSession, session_id)
            task = await db.get(Task, task_id)
            assert await runner._on_task_chat(db, session, task, changed=True) is False
    assert len(page.sent("new_session")) == CHAT_REOPEN_ATTEMPTS
    async with env() as db:
        session = await db.get(ArenaSession, session_id)
        assert session.phase == "needs_human" and CHAT in session.needs_human_reason


async def test_follow_up_into_another_repository_asks_the_owner_at_once(env):  # noqa: F811
    task_id, session_id = await _setup(env, events=[])
    page = Page()
    page.submit_error = ("repo_mismatch", "selected repository is o/other, the task needs o/r")
    runner = ArenaAgentRunner(page)
    async with env() as db:
        session = await db.get(ArenaSession, session_id)
        assert await runner._follow_up(db, session, "pr_nudge", "open the PR") is False
    async with env() as db:
        session, task = await db.get(ArenaSession, session_id), await db.get(Task, task_id)
        assert session.phase == "needs_human" and "follow-up not sent" in session.needs_human_reason
        assert "o/other" in session.needs_human_reason and task.status == "blocked"


async def test_repeated_send_failures_stop_after_the_limit(env):  # noqa: F811
    task_id, session_id = await _setup(env, events=[])
    page = Page()
    page.submit_error = ("send_failed", "text was typed but the page did not send it")
    runner = ArenaAgentRunner(page)
    for attempt in range(1, FOLLOW_UP_FAILURES_BEFORE_HUMAN + 1):
        async with env() as db:
            session = await db.get(ArenaSession, session_id)
            assert await runner._follow_up(db, session, "pr_nudge", "open the PR") is False
        async with env() as db:
            session = await db.get(ArenaSession, session_id)
            expected = "needs_human" if attempt == FOLLOW_UP_FAILURES_BEFORE_HUMAN else "working"
            assert session.phase == expected
    assert "failed 3 times in a row" in session.needs_human_reason


@pytest.mark.parametrize(("path", "branch", "recorded"), [
    (CHAT, None, CHAT),
    (CHAT, "arena/f5fddb3d-aatm", CHAT),
    (CHAT, "arena/ff0f70cb-aatm", None),  # the page shows a chat that is not this task's branch
    ("/agent", None, None),  # the chat URL is not known yet
])
async def test_chat_is_recorded_only_after_aatm_sent_the_prompt(env, path, branch, recorded):  # noqa: F811
    _task_id, session_id = await _setup(env, events=[])
    page = Page(path=path, state="generating")
    runner = ArenaAgentRunner(page)
    async with env() as db:
        session = await db.get(ArenaSession, session_id)
        session.branch_name = branch
        await runner._record_chat_path(session)  # not sent by this process: nothing is recorded
        assert task_chat_path(session) is None and page.commands == []
        runner._chat_state()["unrecorded"].add(session.id)
        await runner._record_chat_path(session)
        assert task_chat_path(session) == recorded


def test_runner_built_without_init_still_has_chat_state(monkeypatch):
    runner = ArenaAgentRunner.__new__(ArenaAgentRunner)
    assert runner._chat_state()["unrecorded"] == set()
    assert runner_module.CHAT_PATH_RE.match(CHAT)
