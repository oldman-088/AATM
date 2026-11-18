"""PATCH /projects/{id} sets the project's team and test command."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.security import create_access_token
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import Project, User
from app.orchestration.manager_arena import ManagerArenaRuntime
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture()
async def factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'team.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await seed_initial_data(db)

    async def override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override
    yield sessions
    app.dependency_overrides.pop(get_db, None)
    await engine.dispose()


async def _setup(factory) -> tuple[dict[str, str], str]:
    async with factory() as db:
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        admin.must_change_password = False
        project = (await db.execute(select(Project))).scalars().first()
        await db.commit()
        token = create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)
        return {"Authorization": f"Bearer {token}"}, project.id


async def test_team_is_saved_and_limits_the_manager(factory) -> None:
    headers, project_id = await _setup(factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.patch(f"/api/v1/projects/{project_id}", headers=headers,
                                 json={"team": ["worker-1", "worker-2", "worker-1"], "test_command": " pytest -q "})
    assert res.status_code == 200, res.text
    assert res.json()["config"]["team"] == ["worker-1", "worker-2"]
    assert res.json()["config"]["test_command"] == "pytest -q"
    async with factory() as db:
        team = await ManagerArenaRuntime()._team(db, project_id)
    assert [member["id"] for member in team] == ["worker-1", "worker-2"]


async def test_unknown_worker_is_rejected(factory) -> None:
    headers, project_id = await _setup(factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.patch(f"/api/v1/projects/{project_id}", headers=headers, json={"team": ["manager-0"]})
    assert res.status_code == 422
    async with factory() as db:
        assert "team" not in ((await db.get(Project, project_id)).config or {})
