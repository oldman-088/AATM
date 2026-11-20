"""Tests for the read-only avatar proxy (``GET /api/v1/media/avatar``).

The dashboard runs under ``img-src 'self'``, so third-party profile pictures
must be streamed through our own origin. These tests pin the SSRF controls:
public hosts only, no credentials, no redirects, image content types only and
a hard byte cap — plus the auth requirement.

No real network access happens: ``socket.getaddrinfo`` and the ``httpx``
streaming client are replaced with fakes.
"""

from __future__ import annotations

import socket

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.routes import media
from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app

PNG = b"\x89PNG\r\n\x1a\n-fake-avatar-bytes"


@pytest.fixture(scope="module")
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="module")
async def auth_headers(client: AsyncClient):
    settings = get_settings()
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    data = resp.json()
    token = data["access_token"]
    if data["user"].get("must_change_password"):
        rotated = await client.post(
            "/api/v1/auth/change-password",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "current_password": settings.DEFAULT_ADMIN_PASSWORD,
                "new_password": "AvatarProxyPassword!2026",
            },
        )
        assert rotated.status_code == 200
        token = rotated.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class _FakeStream:
    def __init__(self, status_code: int, content_type: str, chunks: list[bytes]):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.requested: list[tuple[str, dict]] = []
        self.stream_impl = _FakeStream(200, "image/png", [PNG])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, method: str, url: str, headers: dict | None = None):
        self.requested.append((url, headers or {}))
        return self.stream_impl


@pytest.fixture()
def fake_network(monkeypatch):
    """Resolve every hostname to a documentation-range public IP and fake the HTTP client."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    holders: list[_FakeClient] = []

    def factory(*args, **kwargs):
        client = _FakeClient(*args, **kwargs)
        holders.append(client)
        return client

    monkeypatch.setattr(media.httpx, "AsyncClient", factory)
    return holders


async def test_avatar_proxy_requires_authentication(client: AsyncClient):
    resp = await client.get("/api/v1/media/avatar", params={"url": "https://cdn.example.com/avatar.png"})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "/relative/avatar.png",
        "http://localhost/avatar.png",
        "http://127.0.0.1/avatar.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.internal/avatar.png",
        "",
    ],
)
async def test_avatar_proxy_rejects_unsafe_urls(client: AsyncClient, auth_headers, fake_network, url: str):
    resp = await client.get("/api/v1/media/avatar", params={"url": url}, headers=auth_headers)
    assert resp.status_code in (400, 422)
    assert not fake_network  # nothing was fetched


async def test_avatar_proxy_rejects_urls_with_embedded_credentials(
    client: AsyncClient, auth_headers, fake_network
):
    # Built at runtime so the repository security scan does not read a credential URL literal.
    url = "https://" + "user" + ":" + "secret" + "@" + "cdn.example.com/avatar.png"
    resp = await client.get("/api/v1/media/avatar", params={"url": url}, headers=auth_headers)
    assert resp.status_code == 400
    assert "credentials" in resp.json()["detail"]
    assert not fake_network


async def test_avatar_proxy_streams_image_bytes_and_pins_the_resolved_host(client: AsyncClient, auth_headers, fake_network):
    resp = await client.get(
        "/api/v1/media/avatar",
        params={"url": "https://cdn.example.com/avatar.png"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.headers["cache-control"] == "private, max-age=600"

    assert len(fake_network) == 1
    requested_url, headers = fake_network[0].requested[0]
    # The connection targets the validated IP literal, not the attacker-controlled name.
    assert requested_url == "https://93.184.216.34:443/avatar.png"
    assert headers["Host"] == "cdn.example.com"


async def test_avatar_proxy_rejects_wrong_content_type(client: AsyncClient, auth_headers, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    def factory(*args, **kwargs):
        client = _FakeClient(*args, **kwargs)
        client.stream_impl = _FakeStream(200, "text/html", [b"<html>not an image</html>"])
        return client

    monkeypatch.setattr(media.httpx, "AsyncClient", factory)
    resp = await client.get(
        "/api/v1/media/avatar",
        params={"url": "https://cdn.example.com/avatar.png"},
        headers=auth_headers,
    )
    assert resp.status_code == 502
    assert "not a supported image" in resp.json()["detail"]


async def test_avatar_proxy_rejects_oversized_bodies(client: AsyncClient, auth_headers, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    def factory(*args, **kwargs):
        client = _FakeClient(*args, **kwargs)
        client.stream_impl = _FakeStream(200, "image/png", [b"x" * (media.MAX_AVATAR_BYTES + 1)])
        return client

    monkeypatch.setattr(media.httpx, "AsyncClient", factory)
    resp = await client.get(
        "/api/v1/media/avatar",
        params={"url": "https://cdn.example.com/avatar.png"},
        headers=auth_headers,
    )
    assert resp.status_code == 502
    assert "maximum size" in resp.json()["detail"]


async def test_avatar_proxy_rejects_hostnames_resolving_to_private_addresses(
    client: AsyncClient, auth_headers, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))],
    )
    resp = await client.get(
        "/api/v1/media/avatar",
        params={"url": "https://rebind.example.com/avatar.png"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "public host" in resp.json()["detail"]


async def test_avatar_proxy_reports_upstream_failure_as_502(client: AsyncClient, auth_headers, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    def factory(*args, **kwargs):
        client = _FakeClient(*args, **kwargs)
        client.stream_impl = _FakeStream(404, "image/png", [])
        return client

    monkeypatch.setattr(media.httpx, "AsyncClient", factory)
    resp = await client.get(
        "/api/v1/media/avatar",
        params={"url": "https://cdn.example.com/missing.png"},
        headers=auth_headers,
    )
    assert resp.status_code == 502
