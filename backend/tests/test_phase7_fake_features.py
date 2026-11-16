import asyncio
import base64
import json
import socket
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.browser.engines import BROWSER_ENGINE_REGISTRY
from app.browser.manager import (
    BrowserManager,
    validate_navigation_url,
    validate_public_navigation_url,
)
from app.models import BrowserProfile


class FakeDB:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def add(self, record: Any) -> None:
        self.records.append(record)

    async def commit(self) -> None:
        return None

    async def refresh(self, record: Any) -> None:
        return None


def _profile(profile_id: str = "phase7-profile") -> BrowserProfile:
    return BrowserProfile(
        id=profile_id,
        name="Phase 7 profile",
        profile_dir=f"/tmp/{profile_id}",
        status="running",
        mode="background",
        current_url="about:blank",
    )


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.calls: list[tuple[Any, ...]] = []

    async def goto(self, url: str, timeout: int = 10_000) -> None:
        self.calls.append(("goto", url))
        self.url = url

    async def click(self, selector: str, timeout: int = 5_000) -> None:
        self.calls.append(("click", selector))

    async def fill(self, selector: str, value: str, timeout: int = 5_000) -> None:
        self.calls.append(("fill", selector, value))

    async def type(self, selector: str, value: str, timeout: int = 5_000) -> None:
        self.calls.append(("type", selector, value))

    async def title(self) -> str:
        return "Live test page"

    async def text_content(self, selector: str, timeout: int = 5_000) -> str:
        self.calls.append(("text_content", selector))
        return "Status: ready. Authorization: Bearer " + "a" * 24

    async def screenshot(self, full_page: bool = False, type: str = "png") -> bytes:
        self.calls.append(("screenshot", full_page, type))
        return b"\x89PNG\r\n\x1a\nphase7-test-png"


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]

    async def close(self) -> None:
        return None


class FakePlaywright:
    async def stop(self) -> None:
        return None


def _address_record(address: str, port: int = 443) -> list[tuple[Any, ...]]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


def test_public_navigation_validation_blocks_local_and_private_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate_navigation_url("http://localhost:8080") == "http://localhost:8080"
    assert validate_navigation_url("about:blank") == "about:blank"

    with pytest.raises(ValueError, match="local or internal"):
        asyncio.run(validate_public_navigation_url("http://localhost:8080"))

    with pytest.raises(ValueError, match="private"):
        asyncio.run(validate_public_navigation_url("http://192.168.1.10/admin"))

    with pytest.raises(ValueError, match="credentials"):
        credentialed_url = "https://" + "user:pass@" + "example.test/"
        asyncio.run(validate_public_navigation_url(credentialed_url))

    def resolve_public(host: str, port: int, *args: Any) -> list[tuple[Any, ...]]:
        assert host == "public.example.test"
        return _address_record("93.184.216.34", port)

    monkeypatch.setattr("app.browser.manager.socket.getaddrinfo", resolve_public)

    async def check_public() -> None:
        assert await validate_public_navigation_url("https://public.example.test/path") == (
            "https://public.example.test/path"
        )

    asyncio.run(check_public())

    monkeypatch.setattr(
        "app.browser.manager.socket.getaddrinfo",
        lambda host, port, *args: _address_record("127.0.0.1", port),
    )
    with pytest.raises(ValueError, match="private, link-local"):
        asyncio.run(validate_public_navigation_url("https://rebound.example.test/"))


def test_browser_actions_without_live_context_return_http_501() -> None:

    async def check() -> None:
        manager = BrowserManager()
        db = FakeDB()
        profile = _profile()
        actions = (
            manager.navigate_profile(db, profile, "about:blank"),
            manager.click_element(db, profile, "#submit"),
            manager.fill_input(db, profile, "#query", "secret-value"),
            manager.type_text(db, profile, "#query", "typed-secret"),
            manager.extract_text(db, profile, "main"),
            manager.capture_snapshot(db, profile),
        )
        for action in actions:
            with pytest.raises(HTTPException) as exc_info:
                await action
            assert exc_info.value.status_code == 501
        assert not db.records

        # Every registered adapter must reject an action without a real profile context.
        for adapter in BROWSER_ENGINE_REGISTRY.values():
            with pytest.raises(HTTPException) as exc_info:
                await adapter.execute_action(profile.id, "click", {"selector": "#submit"})
            assert exc_info.value.status_code == 501

            with pytest.raises(HTTPException) as exc_info:
                await adapter.execute_workflow(db, profile, [{"type": "click", "selector": "#submit"}], manager=manager)
            assert exc_info.value.status_code == 501

        with pytest.raises(HTTPException) as exc_info:
            await manager.run_userscript(
                db,
                profile,
                {"id": "stored-script"},
                agent_id="worker-1",
                agent_role="worker",
            )
        assert exc_info.value.status_code == 501

    asyncio.run(check())


def test_live_playwright_actions_return_only_real_operation_results(monkeypatch: pytest.MonkeyPatch) -> None:

    async def allow_test_url(url: str) -> str:
        return url

    monkeypatch.setattr("app.browser.manager.validate_public_navigation_url", allow_test_url)

    async def check() -> None:
        manager = BrowserManager()
        db = FakeDB()
        profile = _profile()
        page = FakePage()
        manager._active_contexts[profile.id] = (FakePlaywright(), FakeContext(page))

        navigated = await manager.navigate_profile(db, profile, "https://public.example.test/page")
        assert navigated.current_url == "https://public.example.test/page"
        assert ("goto", "https://public.example.test/page") in page.calls

        clicked = await manager.click_element(db, profile, "#submit")
        assert clicked["ok"] is True and clicked["executed_live"] is True
        assert ("click", "#submit") in page.calls

        input_value = "private-input-value"
        filled = await manager.fill_input(db, profile, "#query", input_value)
        assert filled["ok"] is True and filled["executed_live"] is True
        assert input_value not in json.dumps(filled)

        typed_value = "typed-private-value"
        typed = await manager.type_text(db, profile, "#query", typed_value)
        assert typed["ok"] is True and typed["executed_live"] is True
        assert typed_value not in json.dumps(typed)

        extracted = await manager.extract_text(db, profile, "main")
        assert extracted["ok"] is True and extracted["executed_live"] is True
        assert "<redacted>" in extracted["text"]
        assert "a" * 24 not in extracted["text"]

        snapshot = await manager.capture_snapshot(db, profile, full_page=True)
        assert snapshot["ok"] is True and snapshot["executed_live"] is True
        assert snapshot["format"] == "png"
        assert base64.b64decode(snapshot["screenshot_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
        assert ("screenshot", True, "png") in page.calls
        assert len(db.records) == 6
        for audit in db.records:
            assert input_value not in json.dumps(audit.details)
            assert typed_value not in json.dumps(audit.details)
            assert "a" * 24 not in json.dumps(audit.details)

    asyncio.run(check())


def test_invalid_or_synthetic_screenshot_is_not_reported_as_success() -> None:

    class InvalidScreenshotPage(FakePage):
        async def screenshot(self, full_page: bool = False, type: str = "png") -> bytes:
            return b"not-a-png"

    async def check() -> None:
        manager = BrowserManager()
        profile = _profile()
        manager._active_contexts[profile.id] = (
            FakePlaywright(),
            FakeContext(InvalidScreenshotPage()),
        )
        with pytest.raises(HTTPException) as exc_info:
            await manager.capture_snapshot(FakeDB(), profile)
        assert exc_info.value.status_code == 502

    asyncio.run(check())


def test_browser_request_guard_aborts_private_network_destinations(monkeypatch: pytest.MonkeyPatch) -> None:

    class Route:
        def __init__(self, url: str, *, is_navigation_request: bool = True) -> None:
            self.request = SimpleNamespace(url=url, is_navigation_request=is_navigation_request)
            self.aborted: str | None = None
            self.continued = False

        async def abort(self, error_code: str) -> None:
            self.aborted = error_code

        async def continue_(self) -> None:
            self.continued = True

    async def check() -> None:
        manager = BrowserManager()
        private_route = Route("http://127.0.0.1/admin")
        await manager._guard_browser_request(private_route)
        assert private_route.aborted == "blockedbyclient"
        assert private_route.continued is False

        # A redirect and a subresource request are independently checked too.
        private_redirect = Route("http://169.254.169.254/latest/meta-data")
        await manager._guard_browser_request(private_redirect)
        assert private_redirect.aborted == "blockedbyclient"
        private_subresource = Route("http://10.0.0.8/analytics.js", is_navigation_request=False)
        await manager._guard_browser_request(private_subresource)
        assert private_subresource.aborted == "blockedbyclient"

        async def allow_public_url(url: str) -> str:
            return url

        monkeypatch.setattr("app.browser.manager.validate_public_navigation_url", allow_public_url)
        public_route = Route("https://public.example.test/")
        await manager._guard_browser_request(public_route)
        assert public_route.continued is True
        assert public_route.aborted is None

    asyncio.run(check())
