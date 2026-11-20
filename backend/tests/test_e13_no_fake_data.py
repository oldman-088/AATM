"""E13 regression: a fresh install must not create demo data, and the demo rows can be removed.

Demo rows are written only when SEED_DEMO_DATA=true. The admin user, agents, providers, project,
settings and audit log are always created. `remove_demo_data` deletes only the demo markers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.browser.arena_connector import MANAGER_PROFILE_ID
from app.core.config import get_settings
from app.db.base import Base
from app.db.demo_cleanup import remove_demo_data
from app.db.seed import seed_initial_data
from app.models import (
    Agent,
    ApprovalRequest,
    AuditLog,
    BrowserProfile,
    ChatMessage,
    GitWorkflowRecord,
    Notification,
    ProviderAccount,
    Task,
    User,
    Userscript,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REAL_PROFILES = {MANAGER_PROFILE_ID, "profile-arena-session"} | {f"profile-worker-{n}" for n in range(5, 9)}


def _set_demo(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # get_settings() is lru-cached, so the flag must be re-read after changing the environment.
    monkeypatch.setenv("SEED_DEMO_DATA", value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    yield
    get_settings.cache_clear()


@pytest.fixture()
async def factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e13.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _count(db, model) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)))


async def _demo_counts(db) -> dict[str, int]:
    return {
        "tasks": await _count(db, Task),
        "chat": await _count(db, ChatMessage),
        "approvals": await _count(db, ApprovalRequest),
        "notifications": await _count(db, Notification),
        "git": await _count(db, GitWorkflowRecord),
        "userscripts": await _count(db, Userscript),
    }


@pytest.mark.asyncio
async def test_fresh_install_creates_only_the_essentials(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_demo(monkeypatch, "false")
    async with factory() as db:
        await seed_initial_data(db)

        assert all(v == 0 for v in (await _demo_counts(db)).values())
        profile_ids = set((await db.execute(select(BrowserProfile.id))).scalars().all())
        assert profile_ids <= REAL_PROFILES

        assert (await db.execute(select(User))).scalars().first() is not None
        account = (await db.execute(select(ProviderAccount))).scalars().one()
        assert account.account_identifier != "aatm-org@arena.ai"
        assert await _count(db, Agent) == 9  # manager + 8 default workers
        assert await _count(db, AuditLog) >= 1


@pytest.mark.asyncio
async def test_demo_data_is_written_only_when_enabled_and_can_be_removed(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_demo(monkeypatch, "true")
    async with factory() as db:
        await seed_initial_data(db)
        before = await _demo_counts(db)
        assert before["tasks"] == 12 and before["chat"] >= 1

        dry = await remove_demo_data(db, apply=False)
        assert dry["tasks"] == before["tasks"]
        assert dry["greeting"] == 1
        assert await _count(db, Task) == before["tasks"]  # dry run deletes nothing

        applied = await remove_demo_data(db, apply=True)
        assert applied["tasks"] == before["tasks"]
        assert applied["browser_profiles"] == 3
        after = await _demo_counts(db)
        assert after["tasks"] == 0 and after["chat"] == 0
        assert await _count(db, ProviderAccount) == 1  # functional: employees' sessions point at it
        assert after["approvals"] == 0 and after["notifications"] == 0 and after["git"] == 0
        assert await _count(db, User) == 1
        assert await _count(db, Agent) == 9
        profile_ids = set((await db.execute(select(BrowserProfile.id))).scalars().all())
        assert profile_ids <= REAL_PROFILES

        again = await remove_demo_data(db, apply=True)
        assert all(v == 0 for v in again.values())


@pytest.mark.asyncio
async def test_removing_demo_data_then_restarting_does_not_bring_the_greeting_back(
    factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_demo(monkeypatch, "false")
    async with factory() as db:
        await seed_initial_data(db)
        await seed_initial_data(db)  # a second start
        assert await _count(db, ChatMessage) == 0
