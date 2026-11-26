"""Owner 2026-11-26: a worker whose PR waits for the manager's review starts its next task at once.

On 2026-11-26 six PRs waited for one manager while seven workers sat idle, because a worker stayed bound
to its PR's Arena session until the merge. Now the waiting session is *parked* (kept open with its chat
URL, not monitored), the worker takes its next task in a new chat, and a correction brings the parked
chat back before it is sent.
"""

# ruff: noqa: F811 - pytest fixtures are re-exported
from __future__ import annotations

from typing import Any

import pytest
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures
from test_r3_arena_agent_workflow import ScriptedPage

from app.browser.arena_sessions import PARKED_PHASE, ArenaSessionConflict, claim_session, open_session_for
from app.db.base import utcnow
from app.models import Agent, ArenaSession, Project, Task
from app.orchestration.arena_runner import ArenaAgentRunner, ArenaRunnerError, arena_reservations

CHAT_A = "/agent/01a216bb-d91f-7acd-9e6e-aaaaaaaaaaaa"


class NavPage(ScriptedPage):
    """A scripted page that also knows which chat the window shows."""

    def __init__(self) -> None:
        super().__init__()
        self.path = "/agent/01a216bb-d91f-7acd-9e6e-bbbbbbbbbbbb"
        self.follows_navigation = True

    async def command(self, profile_id: str, command: str, text: str | None = None, *, timeout: float | None = None,
                      repo: str | None = None, branch: str | None = None, url: str | None = None) -> dict[str, Any]:
        if command == "selector_test":
            return {"page": {"origin": "https://arena.ai", "path": self.path}, "state": "idle"}
        if command == "new_session" and url and self.follows_navigation:
            self.path = url.removeprefix("https://arena.ai")
        return await super().command(profile_id, command, text, timeout=timeout, repo=repo)


async def _worker_with_pr_in_review(env, *, phase: str = "reviewing", chat: bool = True) -> tuple[str, str, str]:
    async with env() as db:
        project = Project(name="Park", slug="park", repo_path="/tmp/none", default_branch="main")
        db.add(project)
        await db.flush()
        agent = await db.get(Agent, "worker-1")
        pr_task = Task(project_id=project.id, title="PR task", description="d", status="in_review", pr_number=6,
                       assigned_agent_id=agent.id, execution_mode="manual")
        nxt = Task(project_id=project.id, title="next task", description="d", status="queued",
                   assigned_agent_id=agent.id, execution_mode="manual")
        db.add_all([pr_task, nxt])
        await db.flush()
        events = [{"ts": utcnow().isoformat(), "kind": "chat_path", "detail": CHAT_A}] if chat else []
        session = ArenaSession(agent_id=agent.id, profile_id=agent.browser_profile_id, task_id=pr_task.id,
                               active_task_id=pr_task.id, phase=phase, page_state="idle", events=events)
        db.add(session)
        agent.status, agent.current_task_id = "idle", None
        await db.commit()
        return pr_task.id, nxt.id, session.id


async def test_a_worker_waiting_for_review_is_not_reserved_and_its_session_is_parked(env):
    pr_id, next_id, session_id = await _worker_with_pr_in_review(env)
    async with env() as db:
        assert "worker-1" not in await arena_reservations(db)
        agent = await db.get(Agent, "worker-1")
        new_session, fresh = await claim_session(db, agent, await db.get(Task, next_id))
        await db.commit()
        assert fresh and new_session.task_id == next_id
    async with env() as db:
        parked = await db.get(ArenaSession, session_id)
        assert parked.is_open and parked.phase == PARKED_PHASE
        assert (await open_session_for(db, parked.profile_id)).task_id == next_id


@pytest.mark.parametrize("phase,chat", [("working", True), ("reviewing", False)])
async def test_a_working_session_or_one_without_a_chat_url_still_holds_the_worker(env, phase, chat):
    _pr_id, next_id, session_id = await _worker_with_pr_in_review(env, phase=phase, chat=chat)
    async with env() as db:
        assert "worker-1" in await arena_reservations(db)
        with pytest.raises(ArenaSessionConflict):
            await claim_session(db, await db.get(Agent, "worker-1"), await db.get(Task, next_id))


async def test_a_correction_brings_the_parked_chat_back_before_anything_is_sent(env, monkeypatch):
    monkeypatch.setattr("app.orchestration.arena_runner.UNPARK_WAIT_SECONDS", 0)
    _pr_id, next_id, session_id = await _worker_with_pr_in_review(env)
    async with env() as db:
        await claim_session(db, await db.get(Agent, "worker-1"), await db.get(Task, next_id))
        current = await open_session_for(db, (await db.get(ArenaSession, session_id)).profile_id)
        current.phase = "reviewing"  # the next task opened its own PR and waits for review too
        current.events = [{"ts": utcnow().isoformat(), "kind": "chat_path",
                           "detail": "/agent/01a216bb-d91f-7acd-9e6e-bbbbbbbbbbbb"}]
        await db.commit()
        current_id = current.id
    page = NavPage()
    runner = ArenaAgentRunner(page)
    async with env() as db:
        await runner._unpark(db, await db.get(ArenaSession, session_id))
    assert page.path == CHAT_A
    assert ("new_session", None) in page.commands
    async with env() as db:
        assert (await db.get(ArenaSession, session_id)).phase == "reviewing"
        assert (await db.get(ArenaSession, current_id)).phase == PARKED_PHASE


async def test_a_window_busy_with_another_task_is_never_taken_for_a_correction(env):
    _pr_id, next_id, session_id = await _worker_with_pr_in_review(env)
    async with env() as db:
        await claim_session(db, await db.get(Agent, "worker-1"), await db.get(Task, next_id))
        current = await open_session_for(db, (await db.get(ArenaSession, session_id)).profile_id)
        current.phase = "working"
        await db.commit()
    page = NavPage()
    async with env() as db:
        with pytest.raises(ArenaRunnerError, match="still working"):
            await ArenaAgentRunner(page)._unpark(db, await db.get(ArenaSession, session_id))
    assert not any(c == "new_session" for c, _ in page.commands)


async def test_the_window_that_does_not_return_to_the_chat_stops_the_correction(env, monkeypatch):
    monkeypatch.setattr("app.orchestration.arena_runner.UNPARK_WAIT_SECONDS", 0)
    _pr_id, next_id, session_id = await _worker_with_pr_in_review(env)
    async with env() as db:
        await claim_session(db, await db.get(Agent, "worker-1"), await db.get(Task, next_id))
        current = await open_session_for(db, (await db.get(ArenaSession, session_id)).profile_id)
        current.phase = "reviewing"
        current.events = [{"ts": utcnow().isoformat(), "kind": "chat_path", "detail": "/agent/x-bbbbbbbbbbbb"}]
        await db.commit()
    page = NavPage()
    page.follows_navigation = False
    async with env() as db:
        with pytest.raises(ArenaRunnerError, match="did not return"):
            await ArenaAgentRunner(page)._unpark(db, await db.get(ArenaSession, session_id))
        assert (await db.get(ArenaSession, session_id)).phase == PARKED_PHASE
