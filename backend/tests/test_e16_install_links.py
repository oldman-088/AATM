"""E16 regression: a one-time, 10-minute install link for one Arena profile's userscript."""

from __future__ import annotations

import pytest

from app.browser.install_links import InstallLinkStore


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_link_works_once() -> None:
    store = InstallLinkStore(ttl_seconds=600, clock=_Clock())
    token, ttl = store.create("profile-x", "admin@example.com")
    assert ttl == 600
    link = store.consume(token)
    assert link is not None and link.profile_id == "profile-x" and link.actor == "admin@example.com"
    assert store.consume(token) is None


def test_a_link_expires_after_ten_minutes() -> None:
    clock = _Clock()
    store = InstallLinkStore(ttl_seconds=600, clock=clock)
    token, _ = store.create("profile-x", "admin@example.com")
    clock.now += 599
    assert store.consume(token) is not None  # still valid just before expiry
    token2, _ = store.create("profile-y", "admin@example.com")
    clock.now += 600
    assert store.consume(token2) is None


def test_unknown_and_empty_tokens_are_refused() -> None:
    store = InstallLinkStore(clock=_Clock())
    assert store.consume("") is None
    assert store.consume("not-a-token") is None


def test_only_a_hash_of_the_token_is_stored() -> None:
    store = InstallLinkStore(clock=_Clock())
    token, _ = store.create("profile-x", "admin@example.com")
    assert token not in store._links
    assert all(len(key) == 64 for key in store._links)


@pytest.mark.asyncio
async def test_install_endpoint_refuses_an_unknown_link(monkeypatch: pytest.MonkeyPatch) -> None:
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    monkeypatch.setattr("app.api.routes.arena.arena_gate", lambda: {"enabled": True, "mode": "mock"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/arena/install/definitely-not-a-real-link")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "install_link_unavailable"
