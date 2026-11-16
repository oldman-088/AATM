"""B2 — Command Bridge UI contract that lives in the backend.

The bridge shows the manager's seeded greeting in the operator's language. That only works if the
seeded message carries a stable marker (`metadata_json.i18n_key`) and the API hands it to the client,
including for databases seeded before 3.2.0.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import ChatMessage

LEGACY_PREFIX = "Chief AI Engineering Manager online."


async def _reset_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def test_seed_marks_the_manager_greeting_as_localisable():
    await _reset_schema()
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        await db.commit()

        messages = list((await db.execute(select(ChatMessage))).scalars().all())
        greetings = [m for m in messages if str(m.content).startswith(LEGACY_PREFIX)]
        assert len(greetings) == 1, "the greeting must be seeded exactly once"
        assert greetings[0].metadata_json["i18n_key"] == "managerWelcome"


async def test_reseeding_repairs_a_legacy_greeting_without_duplicating_it():
    await _reset_schema()
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        await db.commit()

        # Simulate a 3.1 database: the greeting exists, the marker does not.
        greeting = (await db.execute(select(ChatMessage))).scalars().first()
        legacy_meta = {k: v for k, v in dict(greeting.metadata_json or {}).items() if k != "i18n_key"}
        greeting.metadata_json = legacy_meta
        await db.commit()

        await seed_initial_data(db)
        await db.commit()

        messages = list((await db.execute(select(ChatMessage))).scalars().all())
        greetings = [m for m in messages if str(m.content).startswith(LEGACY_PREFIX)]
        assert len(greetings) == 1
        assert greetings[0].metadata_json["i18n_key"] == "managerWelcome"
        # Operator history is untouched and other keys survive the repair.
        assert greetings[0].metadata_json["workers_online"] == 4


async def test_chat_api_exposes_the_i18n_marker_to_the_bridge():
    await _reset_schema()
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        settings = get_settings()
        login = await ac.post(
            "/api/v1/auth/login",
            json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        if login.json()["user"].get("must_change_password"):
            rotated = await ac.post(
                "/api/v1/auth/change-password",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "current_password": settings.DEFAULT_ADMIN_PASSWORD,
                    "new_password": "BridgeContractPassword!2026",
                },
            )
            assert rotated.status_code == 200
            token = rotated.json()["access_token"]

        chats = await ac.get("/api/v1/chat", headers={"Authorization": f"Bearer {token}"})
        assert chats.status_code == 200
        payload = chats.json()
        assert payload, "the seeded greeting must be readable"
        greeting = next(item for item in payload if str(item["content"]).startswith(LEGACY_PREFIX))
        # `/chat` serialises the JSON column as `metadata`; the bridge accepts both spellings.
        assert greeting["metadata"]["i18n_key"] == "managerWelcome"
