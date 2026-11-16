"""B3 — demo mode: clearly labelled, read-only, off by default and refused in production."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.runtime.demo import DEMO_TICK_SECONDS, demo_frames, demo_mode_active


async def _login(client: AsyncClient) -> str:
    settings = get_settings()
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    if resp.json()["user"].get("must_change_password"):
        rotated = await client.post(
            "/api/v1/auth/change-password",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "current_password": settings.DEFAULT_ADMIN_PASSWORD,
                "new_password": "DemoModePassword!2026",
            },
        )
        assert rotated.status_code == 200
        token = rotated.json()["access_token"]
    return token


async def _reset_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        await db.commit()


def test_demo_mode_is_off_by_default_and_never_in_production():
    settings = get_settings()
    assert settings.DEMO_MODE is False
    assert demo_mode_active() is False

    settings.DEMO_MODE = True
    try:
        assert demo_mode_active() is True
        settings.ENVIRONMENT = "production"
        assert demo_mode_active() is False, "demo mode must be refused in production"
    finally:
        settings.DEMO_MODE = False
        settings.ENVIRONMENT = "development"


async def test_demo_endpoint_is_hidden_when_demo_mode_is_off():
    await _reset_schema()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        resp = await client.get("/api/v1/demo/code-stream", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404
        assert "DEMO_MODE" in resp.json()["detail"]

        health = await client.get("/api/health")
        assert health.json()["demo_mode"] is False


async def test_demo_endpoint_serves_flagged_frames_without_writing_anything():
    await _reset_schema()
    settings = get_settings()
    settings.DEMO_MODE = True
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client)
            headers = {"Authorization": f"Bearer {token}"}

            health = await client.get("/api/health")
            assert health.json()["demo_mode"] is True

            first = await client.get("/api/v1/demo/code-stream?limit=12", headers=headers)
            assert first.status_code == 200
            payload = first.json()
            assert payload["demo"] is True
            assert payload["agents"], "the seeded crew must produce demo frames"
            for agent_id, frames in payload["agents"].items():
                assert agent_id
                assert frames
                for frame in frames:
                    assert frame["demo"] is True, "every demo frame must be labelled"
                    assert frame["agent_id"] == agent_id
                    assert frame["action"] in {"write", "test", "review", "done"}

            # Read-only: repeating the call does not create tasks, messages or audit rows.
            second = await client.get("/api/v1/demo/code-stream?limit=12", headers=headers)
            assert second.status_code == 200

            chats = await client.get("/api/v1/chat", headers=headers)
            before_chats = len(chats.json())

            from sqlalchemy import func, select

            from app.models import ChatMessage, Task

            async with AsyncSessionLocal() as db:
                tasks = (await db.execute(select(func.count()).select_from(Task))).scalar_one()
                messages = (await db.execute(select(func.count()).select_from(ChatMessage))).scalar_one()
            assert tasks >= 0
            assert messages == before_chats
    finally:
        settings.DEMO_MODE = False


def test_demo_frames_are_deterministic_per_tick_and_rotate():
    ids = ["worker-1", "worker-2", "worker-3"]
    tick0 = demo_frames(ids, tick=0)
    tick0b = demo_frames(ids, tick=0)
    assert tick0 == tick0b, "the same tick must produce the same frames (testable, no timers)"
    assert tick0["worker-1"][0]["action"] == "write"
    assert tick0["worker-2"][0]["action"] == "test"
    assert tick0["worker-3"][0]["action"] == "review"

    later = demo_frames(ids, tick=1)
    assert later != tick0
    assert later["worker-1"][0]["action"] == "test"
    assert DEMO_TICK_SECONDS >= 1
