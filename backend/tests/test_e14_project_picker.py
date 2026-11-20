"""E14 regression: with several projects, a manager message must name its project.

Without this, the manager planned against whichever project was the default (in practice the AATM
repository itself), so a goal for another product became a plan for AATM.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import Project, User


@pytest.fixture()
async def chat_app(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e14.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await seed_initial_data(db)
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        admin.must_change_password = False
        await db.commit()

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    yield factory
    app.dependency_overrides.pop(get_db, None)
    await engine.dispose()


async def _client(factory) -> AsyncClient:
    async with factory() as db:
        user = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        token = create_access_token(subject=user.email, role=user.role, token_version=user.token_version)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_chat_without_a_project_is_refused_when_several_projects_exist(chat_app) -> None:
    async with chat_app() as db:
        db.add(Project(name="Second product", slug="second-product", description="another product", status="active"))
        await db.commit()
    async with await _client(chat_app) as client:
        response = await client.post("/api/v1/chat", json={"content": "plan a feature"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_required"


@pytest.mark.asyncio
async def test_chat_with_an_unknown_project_is_404(chat_app) -> None:
    async with await _client(chat_app) as client:
        response = await client.post("/api/v1/chat", json={"project_id": "no-such-project", "content": "status"})
    assert response.status_code == 404
