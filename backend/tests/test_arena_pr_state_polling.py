"""E4 regression: a PR merged (or closed) outside AATM must reach the task.

Sessions that wait for a human decision (`reviewing`, `awaiting_approval`) are not monitored any
more, so their pull request can be merged or closed on GitHub without AATM noticing. The runner
polls that PR state (every 5 minutes, configurable): merged -> the task is `merged`, the session is
closed and the queued correction rounds are cancelled; closed without merge -> the task is
`blocked` with a reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.seed import seed_initial_data
from app.git_workflows.manager import git_workflow_manager
from app.models import ArenaSession, BrowserProfile, Project, Task
from app.orchestration.arena_runner import ArenaAgentRunner


@pytest.fixture()
async def sessions(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pr-state.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await seed_initial_data(db)
    yield factory
    await engine.dispose()


def _pr(state: str, number: int = 42) -> dict[str, Any]:
    return {
        "number": number,
        "title": "[AATM-task] Something",
        "url": f"https://github.com/acme/demo/pull/{number}",
        "state": state,
        "isDraft": False,
        "headRefName": "arena/abcdef12-demo",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
    }


async def _waiting_session(db, *, phase: str, task_status: str = "pr_open") -> tuple[ArenaSession, Task]:
    """A session whose PR waits for a human decision."""
    project = (await db.execute(select(Project))).scalars().first()
    project.repo_url = "https://github.com/acme/demo"
    profile = BrowserProfile(id="profile-pr-state", name="pr-state", profile_dir="/tmp/profile-pr-state")
    db.add(profile)
    await db.flush()
    task = Task(project_id=project.id, title="PR waiting for approval", status=task_status, pr_number=42)
    db.add(task)
    await db.flush()
    session = ArenaSession(
        agent_id="worker-1",
        profile_id=profile.id,
        task_id=task.id,
        phase=phase,
        is_open=True,
        branch_name="arena/abcdef12-demo",
        pr_number=42,
        pr_url="https://github.com/acme/demo/pull/42",
    )
    db.add(session)
    await db.commit()
    return session, task


def _mock_github(monkeypatch: pytest.MonkeyPatch, state: str, calls: list[str]) -> None:
    async def _find_task_pull_request(*, repo_slug: str, task_id: str, cwd: Any) -> dict[str, Any]:
        calls.append(task_id)
        return {"ok": True, "pull_request": _pr(state), "count": 1}

    monkeypatch.setattr(git_workflow_manager, "find_task_pull_request", _find_task_pull_request)


@pytest.mark.asyncio
async def test_pr_merged_outside_aatm_moves_the_task_to_merged(sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR merged outside AATM finishes the task within one cycle."""
    calls: list[str] = []
    _mock_github(monkeypatch, "MERGED", calls)
    runner = ArenaAgentRunner()

    async with sessions() as db:
        session, task = await _waiting_session(db, phase="reviewing")
        session_id, task_id = session.id, task.id

        result = await runner.tick(db)

        assert calls == [task_id]
        assert result["pr_states"][session_id]["merged"] is True

        await db.refresh(task)
        await db.refresh(session)
        assert task.status == "merged"
        assert task.pr_status == "merged"
        assert session.is_open is False
        assert session.phase == "merged"
        assert any(e.get("kind") == "pr_merged_outside" for e in session.events or [])


@pytest.mark.asyncio
async def test_pr_merged_outside_aatm_cancels_the_queued_correction_rounds(
    sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correction rounds that are still queued are cancelled when the PR is merged."""
    calls: list[str] = []
    _mock_github(monkeypatch, "MERGED", calls)
    runner = ArenaAgentRunner()

    async with sessions() as db:
        session, task = await _waiting_session(db, phase="awaiting_approval")
        correction = Task(
            project_id=task.project_id,
            title="Correction round",
            status="queued",
            task_type="follow_up",
            parent_task_id=task.id,
        )
        db.add(correction)
        await db.commit()

        await runner.tick(db)

        await db.refresh(correction)
        await db.refresh(task)
        assert correction.status == "cancelled"
        assert task.status == "merged"


@pytest.mark.asyncio
async def test_pr_closed_without_merge_blocks_the_task_with_a_reason(
    sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR closed without merging blocks the task and says why."""
    calls: list[str] = []
    _mock_github(monkeypatch, "CLOSED", calls)
    runner = ArenaAgentRunner()

    async with sessions() as db:
        session, task = await _waiting_session(db, phase="reviewing")

        await runner.tick(db)

        await db.refresh(task)
        await db.refresh(session)
        assert task.status == "blocked"
        assert task.error_message
        assert "closed" in task.error_message.lower()
        assert any(e.get("kind") == "pr_closed_without_merge" for e in session.events or [])


@pytest.mark.asyncio
async def test_pr_state_is_polled_at_most_every_five_minutes(sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    """The poll is throttled: a second cycle inside the interval does not call GitHub again."""
    calls: list[str] = []
    _mock_github(monkeypatch, "OPEN", calls)
    runner = ArenaAgentRunner()

    async with sessions() as db:
        await _waiting_session(db, phase="reviewing")
        await runner.tick(db)
        assert len(calls) == 1

        # A second cycle right after: the PR state is not polled again.
        await runner.tick(db)
        assert len(calls) == 1

        # The interval is configurable.
        runner.pr_state_poll_seconds = 0
        await runner.tick(db)
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_open_pr_keeps_the_task_waiting(sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    """An open PR changes nothing: the task keeps waiting for the owner."""
    calls: list[str] = []
    _mock_github(monkeypatch, "OPEN", calls)
    runner = ArenaAgentRunner()

    async with sessions() as db:
        session, task = await _waiting_session(db, phase="reviewing")
        await runner.tick(db)

        await db.refresh(task)
        await db.refresh(session)
        assert task.status == "pr_open"
        assert session.is_open is True
        assert session.phase == "reviewing"


@pytest.mark.asyncio
async def test_sessions_of_working_employees_are_not_polled(sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the sessions that wait for a human decision are polled for their PR state."""
    calls: list[str] = []
    _mock_github(monkeypatch, "MERGED", calls)
    runner = ArenaAgentRunner()

    async with sessions() as db:
        await _waiting_session(db, phase="working")
        await runner.tick(db)
        assert calls == []


@pytest.mark.asyncio
async def test_a_task_that_is_already_finished_is_not_polled(sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    """A task that is already finished is never touched again."""
    calls: list[str] = []
    _mock_github(monkeypatch, "MERGED", calls)
    runner = ArenaAgentRunner()

    async with sessions() as db:
        _session, task = await _waiting_session(db, phase="reviewing")
        task.status = "merged"
        await db.commit()

        await runner.tick(db)

        assert calls == []
        await db.refresh(task)
        assert task.status == "merged"


def test_the_poll_interval_is_five_minutes_by_default() -> None:
    """The PR state of a waiting session is polled every 5 minutes."""
    from app.orchestration.arena_runner import PR_STATE_POLL_SECONDS

    assert PR_STATE_POLL_SECONDS == 300.0
    assert ArenaAgentRunner().pr_state_poll_seconds == 300.0


def test_first_pr_state_check_is_due_even_on_a_freshly_booted_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """monotonic() can be below 300 s on a fresh CI runner; the first check must still happen."""
    from types import SimpleNamespace

    from app.orchestration import arena_runner as runner_module

    monkeypatch.setattr(runner_module, "time", SimpleNamespace(monotonic=lambda: 10.0))
    runner = ArenaAgentRunner()
    session = SimpleNamespace(id="session-fresh-boot")

    assert runner._pr_state_due(session) is True
    assert runner._pr_state_due(session) is False
