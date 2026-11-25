"""User administration API: admin-only, no secrets in responses, never loses the last active admin."""

# ruff: noqa: F811 - pytest fixtures are re-exported from other test modules
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_e14_project_picker import _client, chat_app  # noqa: F401 - fixture

from app.core.security import create_access_token, hash_password
from app.main import app
from app.models import AuditLog, User


async def _add_user(factory, email: str, role: str) -> str:
    async with factory() as db:
        user = User(email=email, full_name=email.split("@")[0], password_hash=hash_password("Long-enough-pass-123"), role=role)
        db.add(user)
        await db.commit()
        return user.id


async def _client_as(factory, email: str) -> AsyncClient:
    async with factory() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        token = create_access_token(subject=user.email, role=user.role, token_version=user.token_version)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_admin_lists_users_without_password_hashes(chat_app) -> None:
    await _add_user(chat_app, "ops@example.test", "operator")
    async with await _client(chat_app) as client:
        response = await client.get("/api/v1/users")
    assert response.status_code == 200
    users = response.json()
    assert {"ops@example.test"} <= {u["email"] for u in users}
    assert all("password_hash" not in u and "token_version" not in u for u in users)


@pytest.mark.asyncio
async def test_non_admin_cannot_list_or_change_users(chat_app) -> None:
    await _add_user(chat_app, "ops@example.test", "operator")
    target = await _add_user(chat_app, "viewer@example.test", "viewer")
    async with await _client_as(chat_app, "ops@example.test") as client:
        assert (await client.get("/api/v1/users")).status_code == 403
        assert (await client.patch(f"/api/v1/users/{target}", json={"role": "admin"})).status_code == 403


@pytest.mark.asyncio
async def test_role_change_is_audited_and_signs_the_user_out(chat_app) -> None:
    target = await _add_user(chat_app, "viewer@example.test", "viewer")
    async with chat_app() as db:
        before = (await db.get(User, target)).token_version
    async with await _client(chat_app) as client:
        response = await client.patch(f"/api/v1/users/{target}", json={"role": "operator"})
        assert response.status_code == 200 and response.json()["role"] == "operator"
        assert (await client.patch(f"/api/v1/users/{target}", json={"role": "owner"})).status_code == 422
    async with chat_app() as db:
        user = await db.get(User, target)
        assert user.role == "operator" and user.token_version == before + 1
        audit = (await db.execute(select(AuditLog).where(AuditLog.action == "user.update"))).scalars().all()
        assert len(audit) == 1 and audit[0].resource_id == target


@pytest.mark.asyncio
async def test_an_admin_cannot_change_their_own_access_so_one_admin_always_remains(chat_app) -> None:
    async with chat_app() as db:
        me = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        my_id = me.id
    async with await _client(chat_app) as client:
        assert (await client.patch(f"/api/v1/users/{my_id}", json={"is_active": False})).status_code == 409
        assert (await client.patch(f"/api/v1/users/{my_id}", json={"role": "viewer"})).status_code == 409

    # Another admin may demote the first one; it is then the only admin and cannot demote itself.
    other = await _add_user(chat_app, "second@example.test", "admin")
    async with await _client_as(chat_app, "second@example.test") as client:
        assert (await client.patch(f"/api/v1/users/{my_id}", json={"role": "viewer"})).status_code == 200
        assert (await client.patch(f"/api/v1/users/{other}", json={"role": "viewer"})).status_code == 409
    async with chat_app() as db:
        admins = (await db.execute(select(User).where(User.role == "admin", User.is_active.is_(True)))).scalars().all()
        assert [u.id for u in admins] == [other]


@pytest.mark.asyncio
async def test_unknown_user_is_404(chat_app) -> None:
    async with await _client(chat_app) as client:
        assert (await client.patch("/api/v1/users/nope", json={"is_active": False})).status_code == 404
