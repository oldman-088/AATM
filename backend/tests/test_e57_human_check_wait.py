"""E57: a human check on the manager's Arena page pauses a job; it never closes it for good.

Before, the job ended as ``needs_human`` and the owner's request was lost even after the check
was solved. Now the job waits, is marked interrupted (so a prompt that already reached Arena is
read back, not sent twice) and the orchestration tick releases it once the window is ready.
"""

# ruff: noqa: F811 - pytest fixtures are re-exported
from __future__ import annotations

from sqlalchemy import select
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures
from test_r7_manager_arena import scripted  # noqa: F401 - fixture

from app.models import ChatMessage, ManagerJob, Notification
from app.orchestration.manager_arena import HUMAN_CHECK_WAIT_AR


async def test_a_page_check_pauses_the_job_and_tells_the_owner_once(env, scripted):
    make, project_id = scripted
    _page, runtime = make()
    async with env() as db:
        job = await runtime.enqueue(db, "plan", project_id=project_id, text="build the app")
        job.status, job.attempts = "running", 1
        await db.commit()
        job_id = job.id
    for _ in range(2):  # the check shows twice before it is solved
        async with env() as db:
            await runtime._wait_for_human_check(db, await db.get(ManagerJob, job_id), "Arena page needs a human: captcha")
    async with env() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "waiting_connection"
        assert job.attempts == 0, "a human check is not a failed attempt"
        assert job.context["interrupted"] is True
        replies = [m.content for m in (await db.execute(select(ChatMessage))).scalars() if m.content == HUMAN_CHECK_WAIT_AR]
        assert len(replies) == 1
        alerts = [n for n in (await db.execute(select(Notification))).scalars() if "فحص" in (n.title or "")]
        assert len(alerts) == 1


async def test_the_tick_releases_the_job_when_the_window_is_ready_again(env, scripted, monkeypatch):
    make, project_id = scripted
    _page, runtime = make()
    async with env() as db:
        job = await runtime.enqueue(db, "plan", project_id=project_id, text="build the app")
        await runtime._wait_for_human_check(db, job, "Arena page needs a human: captcha")
        job_id = job.id

    async def ready(_profile_id):
        return {"connected": True, "code": "ready", "reason_ar": ""}

    monkeypatch.setattr("app.orchestration.manager_arena.manager_readiness", ready)
    monkeypatch.setattr(runtime, "kick", lambda: None)
    async with env() as db:
        result = await runtime.tick(db)
    assert result["released"] >= 1
    async with env() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "queued"
        assert "human_check_waiting" not in job.context, "a later check must alert the owner again"
