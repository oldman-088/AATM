"""P0 / E52: a connection error that keeps repeating is shown to the owner with its cause, and alerted once."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_r7_manager_arena
from __future__ import annotations

from sqlalchemy import select
from test_r7_manager_arena import (  # noqa: F401 - fixtures
    arena_settings,
    env,
    scripted,
)

from app.models import ChatMessage, ManagerJob, Notification
from app.orchestration.manager_arena import NOT_CONNECTED_AR, REPEATED_ERROR_ALERT


async def _alerts(db) -> list[Notification]:
    rows = (await db.execute(select(Notification).where(Notification.category == "arena"))).scalars().all()
    return [n for n in rows if "الخطأ نفسه" in n.title]


async def test_same_error_is_surfaced_after_three_times_and_alerted_once(env, scripted):
    make, project_id = scripted
    _, runtime = make()
    async with env() as db:
        job = await runtime.enqueue(db, "chat", project_id=project_id, text="hello", chat_message_id=None)
        for _ in range(REPEATED_ERROR_ALERT - 1):
            await runtime._connection_error(db, job, "composer_not_found", "composer_not_found: no composer on the page")
        reply = await db.get(ChatMessage, job.reply_message_id)
        assert reply.content == NOT_CONNECTED_AR and await _alerts(db) == []

        for _ in range(2):
            await runtime._connection_error(db, job, "composer_not_found", "composer_not_found: no composer on the page")
        job = await db.get(ManagerJob, job.id)
        assert job.status == "waiting_connection" and job.context["same_error_count"] == REPEATED_ERROR_ALERT + 1
        reply = await db.get(ChatMessage, job.reply_message_id)
        assert "composer_not_found" in reply.content and str(REPEATED_ERROR_ALERT + 1) in reply.content
        assert len(await _alerts(db)) == 1


async def test_a_different_error_restarts_the_count(env, scripted):
    make, project_id = scripted
    _, runtime = make()
    async with env() as db:
        job = await runtime.enqueue(db, "chat", project_id=project_id, text="hello", chat_message_id=None)
        await runtime._connection_error(db, job, "composer_not_found", "x")
        await runtime._connection_error(db, job, "composer_not_found", "x")
        await runtime._connection_error(db, job, "bridge_unreachable", "y")
        assert job.context["same_error_count"] == 1 and job.context["last_error_code"] == "bridge_unreachable"
        assert await _alerts(db) == []
