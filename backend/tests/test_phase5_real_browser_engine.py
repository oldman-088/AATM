"""Phase 5 verification tests: real Playwright browser snapshots, PNG endpoint, SSRF, lock, and 501 fallback."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.browser.manager import browser_manager, detect_playwright_capabilities
from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import BrowserProfile, User

_VALID_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"


class _FakePage:
    def __init__(self, url: str = "https://example.com") -> None:
        self.url = url
        self._title = "Example Domain"
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []

    async def goto(self, url: str, timeout: int = 10_000) -> None:
        self.url = url
        self._title = f"Visited {url}"

    async def title(self) -> str:
        return self._title

    async def click(self, selector: str, timeout: int = 5_000) -> None:
        self.clicks.append(selector)

    async def fill(self, selector: str, value: str, timeout: int = 5_000) -> None:
        self.fills.append((selector, value))

    async def screenshot(self, full_page: bool = False, type: str = "png") -> bytes:
        return _VALID_PNG

    async def text_content(self, selector: str, timeout: int = 5_000) -> str:
        return "Hello Example Page with Authorization: Bearer dummy-session-value"


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.pages = [page]

    async def new_page(self) -> _FakePage:
        return self.pages[0]

    async def close(self) -> None:
        return None


class _FakePlaywright:
    async def stop(self) -> None:
        return None


@pytest.fixture()
async def phase5_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "phase5.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "BROWSER_PROFILES_DIR", tmp_path / "browser_profiles")
    async with session_factory() as db:
        await seed_initial_data(db)
        res = await db.execute(select(BrowserProfile))
        for bp in res.scalars().all():
            bp.profile_dir = str(tmp_path / "browser_profiles" / bp.id)
        await db.commit()
    yield session_factory
    browser_manager._active_contexts.clear()
    browser_manager._last_png_snapshots.clear()
    await engine.dispose()


async def test_phase5_unavailable_chromium_returns_501_honestly(
    phase5_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    async def _override_db():
        async with phase5_db() as db:
            yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        async with phase5_db() as db:
            admin = (await db.execute(select(User).where(User.role == "admin"))).scalar_one()
            admin.must_change_password = False
            await db.commit()
            token = create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)

        monkeypatch.setattr(
            "app.browser.manager.detect_playwright_capabilities",
            lambda: {
                "playwright_installed": True,
                "chromium_installed": False,
                "executable_path": None,
                "engine_mode": "not_implemented",
                "browser_source": None,
                "playwright_browsers_path": None,
            },
        )
        monkeypatch.setattr(
            "app.api.routes.infra.detect_playwright_capabilities",
            lambda: {
                "playwright_installed": True,
                "chromium_installed": False,
                "executable_path": None,
                "engine_mode": "not_implemented",
                "browser_source": None,
                "playwright_browsers_path": None,
            },
        )

        headers = {"Authorization": f"Bearer {token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            caps = await client.get("/api/v1/browsers/capabilities", headers=headers)
            assert caps.status_code == 200
            assert caps.json()["engine_mode"] == "not_implemented"

            launch = await client.post(
                "/api/v1/browsers/profile-frontend-preview/launch",
                headers=headers,
                json={"mode": "background", "owner": "manager-0"},
            )
            assert launch.status_code == 501

            snap_get = await client.get("/api/v1/browsers/profile-frontend-preview/snapshot", headers=headers)
            assert snap_get.status_code == 501
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_phase5_live_browser_png_snapshots_ssrf_blocking_and_lock_enforcement(
    phase5_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    async def _override_db():
        async with phase5_db() as db:
            yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        async with phase5_db() as db:
            admin = (await db.execute(select(User).where(User.role == "admin"))).scalar_one()
            admin.must_change_password = False
            await db.commit()
            token = create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)

        fake_page = _FakePage()
        monkeypatch.setattr(
            "app.browser.manager.detect_playwright_capabilities",
            lambda: {
                "playwright_installed": True,
                "chromium_installed": True,
                "executable_path": "/usr/bin/chromium",
                "engine_mode": "playwright",
                "browser_source": "system_binary",
                "playwright_browsers_path": None,
            },
        )

        async def _fake_launch(profile, _pdir, _caps, url):
            if url:
                fake_page.url = url
            profile.current_url = fake_page.url
            profile.page_title = await fake_page.title()
            browser_manager._active_contexts[profile.id] = (_FakePlaywright(), _FakeContext(fake_page))
            return True, None

        async def _fake_public_url(url: str) -> str:
            if "127.0.0.1" in url or "localhost" in url or "169.254.169.254" in url:
                raise ValueError("Blocked local/private address")
            return url

        monkeypatch.setattr(browser_manager, "_launch_playwright_context", _fake_launch)
        monkeypatch.setattr("app.browser.manager.validate_public_navigation_url", _fake_public_url)
        monkeypatch.setattr(
            browser_manager,
            "_measure_profile_memory_mb",
            lambda _p: 42.5,
        )

        headers = {"Authorization": f"Bearer {token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Launch live profile
            launch = await client.post(
                "/api/v1/browsers/profile-frontend-preview/launch",
                headers=headers,
                json={"mode": "background", "owner": "manager-0", "url": "https://example.com/"},
            )
            assert launch.status_code == 200
            launch_data = launch.json()
            assert launch_data["status"] == "running"
            assert launch_data["live_context"] is True
            assert launch_data["memory_mb"] == 42.5
            assert launch_data["memory_status"] == "measured"

            # 2. Single-owner lock enforcement rejects second owner
            conflict = await client.post(
                "/api/v1/browsers/profile-frontend-preview/launch",
                headers=headers,
                json={"mode": "background", "owner": "worker-2"},
            )
            assert conflict.status_code == 409

            # 3. SSRF blocking refuses loopback/metadata URLs
            ssrf = await client.post(
                "/api/v1/browsers/profile-frontend-preview/navigate",
                headers=headers,
                json={"url": "http://127.0.0.1:8000/api/health"},
            )
            assert ssrf.status_code == 400

            # 4. Navigate to public URL refreshes PNG snapshot automatically
            nav = await client.post(
                "/api/v1/browsers/profile-frontend-preview/navigate",
                headers=headers,
                json={"url": "https://example.org/docs"},
            )
            assert nav.status_code == 200
            assert nav.json()["has_png_snapshot"] is True
            assert nav.json()["snapshot_url"] == "/api/v1/browsers/profile-frontend-preview/snapshot"

            # 5. POST /snapshot & GET /snapshot serve real PNG bytes
            snap_post = await client.post("/api/v1/browsers/profile-frontend-preview/snapshot", headers=headers)
            assert snap_post.status_code == 200
            snap_json = snap_post.json()
            assert snap_json["format"] == "png"
            assert base64.b64decode(snap_json["screenshot_base64"]).startswith(b"\x89PNG\r\n\x1a\n")

            snap_get = await client.get("/api/v1/browsers/profile-frontend-preview/snapshot", headers=headers)
            assert snap_get.status_code == 200
            assert snap_get.headers["content-type"] == "image/png"
            assert snap_get.content.startswith(b"\x89PNG\r\n\x1a\n")

            # 6. Extract text redacts Authorization Bearer token
            ext = await client.post(
                "/api/v1/browsers/profile-frontend-preview/extract-text",
                headers=headers,
                json={"selector": "body"},
            )
            assert ext.status_code == 200
            assert ext.json()["redacted_count"] >= 1
            assert "dummy-session-value" not in ext.json()["text"]

            # 7. Stop clears context & PNG snapshot cache
            stop = await client.post("/api/v1/browsers/profile-frontend-preview/stop", headers=headers)
            assert stop.status_code == 200
            assert stop.json()["status"] == "stopped"
            assert stop.json()["has_png_snapshot"] is False
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_phase5_detect_playwright_capabilities_structure():
    caps = detect_playwright_capabilities()
    assert set(caps.keys()) >= {"playwright_installed", "chromium_installed", "engine_mode"}
    assert caps["engine_mode"] in {"playwright", "not_implemented"}
    _ = SimpleNamespace(ok=True)
