import asyncio
import base64
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.browser.engines import (
    BROWSER_ENGINE_REGISTRY,
    AutomaWorkflowAdapter,
    authorize_userscript_execution,
    match_url_pattern,
    validate_userscript_policy,
)
from app.browser.manager import (
    BrowserManager,
    browser_manager,
    detect_playwright_capabilities,
    redact_extracted_text,
    validate_navigation_url,
    validate_selector,
)
from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.employees.catalog import analyze_team_skill_gaps, provision_missing_specialists
from app.models import (
    Agent,
    AIProvider,
    AuditLog,
    BrowserProfile,
    BrowserSession,
    Project,
    ProviderAccount,
    ProviderSession,
    ProviderUsageLog,
    SystemSetting,
    Task,
)
from app.orchestration.replacement import replacement_service
from app.providers.adapters import AnthropicClaudeAdapter, ArenaLocalBridgeAdapter
from app.providers.base import ProviderRequest, validate_credential_ref
from app.providers.router import ProviderRouter
from app.runtime.worker_daemon import WorkerConfig, WorkerDaemon


class _MockBridgeServer:
    """Real loopback HTTP server implementing the Arena Local Bridge & Anthropic Messages test contracts."""

    def __init__(self, expected_bearer: str | None = None, expected_api_key: str | None = None):
        self.expected_bearer = expected_bearer
        self.expected_api_key = expected_api_key
        self.empty_choices = False
        self.sessions: dict[str, str] = {}
        self.received_headers: list[dict[str, str]] = []
        self.received_payloads: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url: str = ""

    def __enter__(self) -> "_MockBridgeServer":
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:
                parent.received_headers.append({k.lower(): v for k, v in self.headers.items()})
                if self.path == "/health":
                    if parent.expected_bearer and self.headers.get("Authorization") != f"Bearer {parent.expected_bearer}":
                        self.send_response(401)
                        self.end_headers()
                        self.wfile.write(b'{"error":"unauthorized"}')
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok","models":["arena-bridge-default"]}')
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8"))
                parent.received_headers.append({k.lower(): v for k, v in self.headers.items()})
                parent.received_payloads.append(payload)

                if self.path == "/v1/chat/completions":
                    if parent.expected_bearer and self.headers.get("Authorization") != f"Bearer {parent.expected_bearer}":
                        self.send_response(401)
                        self.end_headers()
                        return
                    body = {
                        "id": "chatcmpl-test",
                        "model": payload.get("model", "arena-bridge-default"),
                        "choices": []
                        if parent.empty_choices
                        else [{"index": 0, "message": {"role": "assistant", "content": "Bridge live pong"}}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(body).encode("utf-8"))
                    return

                if self.path == "/v1/messages":
                    if parent.expected_api_key and self.headers.get("x-api-key") != parent.expected_api_key:
                        self.send_response(401)
                        self.end_headers()
                        return
                    body = {
                        "id": "msg-test",
                        "type": "message",
                        "model": payload.get("model", "claude-sonnet-4-5"),
                        "content": [{"type": "text", "text": "Claude live pong"}],
                        "usage": {"input_tokens": 20, "output_tokens": 10},
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(body).encode("utf-8"))
                    return

                self.send_response(404)
                self.end_headers()

            def do_DELETE(self):  # noqa: N802
                parent.received_headers.append({k.lower(): v for k, v in self.headers.items()})
                if self.path.startswith("/v1/sessions/"):
                    if parent.expected_bearer and self.headers.get("Authorization") != f"Bearer {parent.expected_bearer}":
                        self.send_response(401)
                        self.end_headers()
                        return
                    sid = self.path.removeprefix("/v1/sessions/")
                    if sid not in parent.sessions:
                        self.send_response(404)
                        self.end_headers()
                        return
                    req_acct = self.headers.get("X-AATM-Account-Id")
                    owner_acct = parent.sessions[sid]
                    if req_acct and req_acct != owner_acct:
                        self.send_response(403)
                        self.end_headers()
                        return
                    del parent.sessions[sid]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"deleted": True, "session_id": sid}).encode("utf-8"))
                    return
                self.send_response(404)
                self.end_headers()

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
async def db_session():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        yield db


def _enable_fake_live_browser(monkeypatch, manager: BrowserManager) -> None:
    """Provide a deterministic live-context harness without installing Chromium in CI."""
    monkeypatch.setattr(
        "app.browser.manager.detect_playwright_capabilities",
        lambda: {
            "playwright_installed": True,
            "chromium_installed": True,
            "executable_path": None,
            "browser_source": "test-harness",
            "engine_mode": "playwright",
        },
    )

    async def allow_public_url(url: str) -> str:
        return url

    monkeypatch.setattr("app.browser.manager.validate_public_navigation_url", allow_public_url)

    class FakePage:
        def __init__(self, url: str):
            self.url = url

        async def title(self) -> str:
            return "Test browser page"

    class FakeContext:
        def __init__(self, page: FakePage):
            self.pages = [page]

        async def close(self) -> None:
            return None

    class FakePlaywright:
        async def stop(self) -> None:
            return None

    async def fake_launch(profile, _profile_dir, _caps, url):
        page = FakePage(url or profile.current_url or "about:blank")
        profile.current_url = page.url
        profile.page_title = "Test browser page"
        manager._active_contexts[profile.id] = (FakePlaywright(), FakeContext(page))
        return True, None

    monkeypatch.setattr(manager, "_launch_playwright_context", fake_launch)


# ---------------------------------------------------------------------------
# Division 05: AI Providers, Credential Indirection, Budgets & Failover
# ---------------------------------------------------------------------------


async def test_credential_ref_indirection_blocks_raw_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-env-indirection")
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "bridge-test-env-indirection")
    assert validate_credential_ref(None)[0] is False
    assert validate_credential_ref("env:ANTHROPIC_API_KEY")[0] is True
    assert validate_credential_ref("env:ARENA_BRIDGE_TOKEN")[0] is True

    raw_secret = "raw-inline-secret-key-NEVER-LEAK"
    ok, msg = validate_credential_ref(raw_secret)
    assert ok is False
    assert raw_secret not in msg

    assert validate_credential_ref("file:/etc/passwd")[0] is False
    assert validate_credential_ref("env:lowercase_invalid")[0] is False

    adapter = AnthropicClaudeAdapter("anthropic-bad", "https://api.anthropic.com/v1", raw_secret, "claude-sonnet-4-5")
    health = await adapter.health_check()
    assert health.status == "degraded"
    assert health.credentials_configured is False
    assert raw_secret not in health.detail

    resp = await adapter.generate(ProviderRequest(prompt="hello", dry_run=False))
    assert resp.success is False
    assert resp.estimated_cost_usd == 0.0
    assert raw_secret not in (resp.error_message or "")


async def test_arena_local_bridge_live_auth_and_offline(monkeypatch):
    monkeypatch.setenv("ARENA_TEST_BRIDGE_TOKEN", "bridge-secret-123")
    with _MockBridgeServer(expected_bearer="bridge-secret-123") as srv:
        adapter = ArenaLocalBridgeAdapter(
            "arena-local-bridge",
            srv.base_url,
            "env:ARENA_TEST_BRIDGE_TOKEN",
            "arena-bridge-default",
        )
        health = await adapter.health_check()
        assert health.status == "healthy"
        assert health.credentials_configured is True

        # Dry-run never spends budget and never fabricates a successful completion
        dry = await adapter.generate(ProviderRequest(prompt="test", model="arena-reasoning-pro", dry_run=True))
        assert dry.success is False
        assert dry.estimated_cost_usd == 0.0
        assert dry.metadata["dry_run"] is True
        assert "Dry run" in (dry.error_message or "")

        # Live call reads OpenAI-compatible usage & content
        live = await adapter.generate(ProviderRequest(prompt="ping", dry_run=False))
        assert live.success is True
        assert live.content == "Bridge live pong"
        assert live.input_tokens == 11
        assert live.output_tokens == 7
        assert live.metadata["confirmed_usage"] is True

        # Empty choices array handled gracefully without IndexError and without fabricating success
        srv.empty_choices = True
        empty_resp = await adapter.generate(ProviderRequest(prompt="ping", dry_run=False))
        assert empty_resp.success is False
        assert empty_resp.content == ""
        assert empty_resp.metadata.get("empty_response") is True

        # Wrong token yields degraded auth error on health check
        monkeypatch.setenv("ARENA_TEST_BRIDGE_TOKEN", "wrong-token")
        bad_health = await adapter.health_check()
        assert bad_health.status == "degraded"
        assert "401" in bad_health.detail

    # Offline bridge after server shutdown
    offline_adapter = ArenaLocalBridgeAdapter("arena-local-bridge", srv.base_url, "env:ARENA_TEST_BRIDGE_TOKEN", "arena-bridge-default")
    off_health = await offline_adapter.health_check()
    assert off_health.status == "offline"
    assert srv.base_url in off_health.detail

    off_gen = await offline_adapter.generate(ProviderRequest(prompt="ping", dry_run=False))
    assert off_gen.success is False
    assert off_gen.metadata.get("bridge_offline") is True


async def test_provider_router_forwards_employee_session_and_account_metadata(db_session, monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "bridge-meta-secret")
    with _MockBridgeServer(expected_bearer="bridge-meta-secret") as srv:
        bridge = await db_session.get(AIProvider, "arena-local-bridge")
        bridge.endpoint_url = srv.base_url
        await db_session.commit()

        router = ProviderRouter()
        resp = await router.route_and_execute(
            db_session,
            ProviderRequest(prompt="Check session forwarding", agent_id="worker-3", dry_run=False),
            preferred_provider_id="arena-local-bridge",
        )
        assert resp.success is True
        assert resp.metadata["session_id"] == "sess-worker-3"
        assert resp.metadata["account_id"] == "acct-arena-primary"

        # Verify both headers and JSON metadata carried ProviderSession.id & account_id
        assert srv.received_headers[-1]["x-aatm-session-id"] == "sess-worker-3"
        assert srv.received_headers[-1]["x-aatm-account-id"] == "acct-arena-primary"
        assert srv.received_headers[-1]["x-aatm-agent-id"] == "worker-3"
        assert srv.received_payloads[-1]["session_id"] == "sess-worker-3"
        assert srv.received_payloads[-1]["account_id"] == "acct-arena-primary"
        assert srv.received_payloads[-1]["metadata"]["session_id"] == "sess-worker-3"
        assert srv.received_payloads[-1]["metadata"]["account_id"] == "acct-arena-primary"

        # Verify ProviderSession telemetry incremented
        sess = await db_session.get(ProviderSession, "sess-worker-3")
        assert sess.message_count == 1
        assert sess.last_message_at is not None

        # Verify explicit ProviderRequest.metadata overrides are preserved
        explicit = await router.route_and_execute(
            db_session,
            ProviderRequest(
                prompt="Explicit override check",
                agent_id="worker-3",
                dry_run=False,
                metadata={"session_id": "sess-custom-override", "account_id": "acct-custom"},
            ),
            preferred_provider_id="arena-local-bridge",
        )
        assert explicit.success is True
        assert srv.received_headers[-1]["x-aatm-session-id"] == "sess-custom-override"
        assert srv.received_headers[-1]["x-aatm-account-id"] == "acct-custom"


async def test_anthropic_safe_diagnostic_and_live_confirmed_usage(db_session, monkeypatch):
    # Without API key -> safe zero-spend diagnostic mode
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter = AnthropicClaudeAdapter("anthropic-claude", None, "env:ANTHROPIC_API_KEY", "claude-sonnet-4-5")
    h = await adapter.health_check()
    assert h.status == "degraded"
    assert h.credentials_configured is False

    diag = await adapter.generate(ProviderRequest(prompt="Analyze architecture", dry_run=False))
    assert diag.success is False
    assert diag.estimated_cost_usd == 0.0
    assert diag.metadata["credential_not_configured"] is True

    # With API key + loopback mock Anthropic endpoint -> live confirmed usage logged
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    with _MockBridgeServer(expected_api_key="test-anthropic-key") as srv:
        p = await db_session.get(AIProvider, "anthropic-claude")
        p.endpoint_url = f"{srv.base_url}/v1"
        await db_session.commit()

        router = ProviderRouter()
        resp = await router.route_and_execute(
            db_session,
            ProviderRequest(prompt="Live check", dry_run=False, agent_id="worker-3"),
            preferred_provider_id="anthropic-claude",
        )
        assert resp.success is True
        assert resp.content == "Claude live pong"
        assert resp.input_tokens == 20
        assert resp.output_tokens == 10
        assert resp.estimated_cost_usd > 0.0

        logs = (await db_session.execute(select(ProviderUsageLog).where(ProviderUsageLog.provider_id == "anthropic-claude"))).scalars().all()
        assert len(logs) == 1
        assert logs[0].estimated_cost_usd == resp.estimated_cost_usd
        assert logs[0].confirmed_cost_usd is None
        assert logs[0].input_tokens == 20
        assert logs[0].input_tokens_estimated is False


async def test_router_daily_monthly_spend_caps_and_failover(db_session, monkeypatch):
    router = ProviderRouter()
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "phase2-bridge-token")
    monkeypatch.setenv("PHASE2_ANTHROPIC_KEY", "phase2-anthropic-key")

    # Point the bridge to an unused loopback port and use a live local Anthropic mock
    # as the real failover target; the arena_agent provider needs a connected Arena Agent profile.
    bridge = await db_session.get(AIProvider, "arena-local-bridge")
    bridge.endpoint_url = "http://127.0.0.1:1"
    anthropic = await db_session.get(AIProvider, "anthropic-claude")
    assert anthropic is not None
    anthropic.endpoint_url = "http://127.0.0.1:9/v1"
    anthropic.credential_ref = "env:PHASE2_ANTHROPIC_KEY"
    await db_session.commit()

    with _MockBridgeServer(expected_api_key="phase2-anthropic-key") as srv:
        anthropic.endpoint_url = f"{srv.base_url}/v1"
        await db_session.commit()

        failover = await router.route_and_execute(
            db_session,
            ProviderRequest(prompt="Failover test", model="arena-reasoning-pro", dry_run=False),
            preferred_provider_id="arena-local-bridge",
        )
        assert failover.success is True
        assert failover.provider_id == "anthropic-claude"
        assert failover.model == "claude-sonnet-4-5"

    await db_session.refresh(bridge)
    assert bridge.health_status == "offline"

    # When ONLY offline local bridge is enabled, safe diagnostic fallback engages cleanly when allow_diagnostic_fallback=True
    for pid in ("arena-agent", "anthropic-claude"):
        prov = await db_session.get(AIProvider, pid)
        prov.is_enabled = False
    await db_session.commit()

    diag = await router.route_and_execute(
        db_session,
        ProviderRequest(prompt="Bridge offline check", dry_run=False, allow_diagnostic_fallback=True),
    )
    assert diag.success is True
    assert diag.estimated_cost_usd == 0.0
    assert diag.metadata["mode"] == "bridge_offline_diagnostic"

    strict_fail = await router.route_and_execute(
        db_session,
        ProviderRequest(prompt="Bridge offline strict", dry_run=False, allow_diagnostic_fallback=False),
    )
    assert strict_fail.success is False

    # Re-enable all providers and test daily & monthly spend caps
    for pid in ("arena-agent", "anthropic-claude"):
        prov = await db_session.get(AIProvider, pid)
        prov.is_enabled = True

    # Configure tight monthly budget cap ($1.00) and record $1.05 spend
    budgets = await db_session.get(SystemSetting, "budgets")
    budgets.value = {"daily_limit_usd": 20.0, "monthly_limit_usd": 1.0, "require_approval_above_usd": 5.0}
    db_session.add(
        ProviderUsageLog(
            provider_id="anthropic-claude",
            model_name="claude-sonnet-4-5",
            input_tokens=1000,
            output_tokens=1000,
            estimated_cost_usd=1.05,
            latency_ms=50.0,
            success=True,
        )
    )
    await db_session.commit()

    summary = await router.get_spend_summary(db_session)
    assert summary["global_monthly_spend_usd"] >= 1.05
    assert summary["monthly_remaining_usd"] == 0.0

    # Live call strictly blocked by monthly spend cap
    blocked_monthly = await router.route_and_execute(
        db_session,
        ProviderRequest(prompt="Should block on monthly cap", dry_run=False),
        preferred_provider_id="anthropic-claude",
    )
    assert blocked_monthly.success is False
    assert blocked_monthly.metadata.get("budget_exceeded") is True
    assert "monthly budget cap" in (blocked_monthly.error_message or "").lower()

    # Dry-run remains permitted even when monthly cap is reached and adds $0 spend
    dry_ok = await router.route_and_execute(
        db_session,
        ProviderRequest(prompt="Dry-run diagnostic", dry_run=True),
        preferred_provider_id="anthropic-claude",
    )
    assert dry_ok.success is True
    assert dry_ok.content == "Dry run: no provider request was sent."
    assert dry_ok.estimated_cost_usd == 0.0
    assert dry_ok.metadata == {"dry_run": True, "mode": "dry_run"}

    dry_stream = [
        chunk
        async for chunk in router.route_and_stream(
            db_session,
            ProviderRequest(prompt="Dry-run streaming diagnostic", dry_run=True),
            preferred_provider_id="anthropic-claude",
        )
    ]
    assert len(dry_stream) == 2
    assert dry_stream[0].delta == "Dry run: no provider request was sent."
    assert dry_stream[0].done is False
    assert dry_stream[1].done is True and dry_stream[1].success is True
    assert dry_stream[1].estimated_cost_usd == 0.0
    assert dry_stream[1].metadata["mode"] == "dry_run"

    after_dry = await router.get_spend_summary(db_session)
    assert after_dry["global_monthly_spend_usd"] == summary["global_monthly_spend_usd"]

    # Raise monthly cap to $100, lower global daily cap to $1.00 -> blocked by global daily cap
    budgets.value = {"daily_limit_usd": 1.0, "monthly_limit_usd": 100.0, "require_approval_above_usd": 5.0}
    await db_session.commit()
    blocked_daily = await router.route_and_execute(
        db_session,
        ProviderRequest(prompt="Should block on global daily cap", dry_run=False),
    )
    assert blocked_daily.success is False
    assert blocked_daily.metadata.get("budget_exceeded") is True
    assert "daily budget cap" in (blocked_daily.error_message or "").lower()


# ---------------------------------------------------------------------------
# Division 06: Browser Lifecycle, DB+File+Async Locking & Crash Recovery
# ---------------------------------------------------------------------------


def test_playwright_capability_detection_and_url_validation(tmp_path, monkeypatch):
    caps = detect_playwright_capabilities()
    assert caps["playwright_installed"] is True
    assert caps["engine_mode"] in ("playwright", "not_implemented")
    assert "browser_source" in caps

    # Verify PLAYWRIGHT_BROWSERS_PATH discovery works cleanly
    fake_cache = tmp_path / "ms-playwright" / "chromium-1234" / "chrome-linux"
    fake_cache.mkdir(parents=True)
    fake_chrome = fake_cache / "chrome"
    fake_chrome.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_chrome.chmod(0o755)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "ms-playwright"))

    custom_caps = detect_playwright_capabilities()
    assert custom_caps["chromium_installed"] is True
    assert custom_caps["browser_source"] == "playwright_cache"
    assert custom_caps["executable_path"] == str(fake_chrome)

    # URL scheme safety
    assert validate_navigation_url("https://fastapi.tiangolo.com") == "https://fastapi.tiangolo.com"
    assert validate_navigation_url("http://localhost:5173") == "http://localhost:5173"
    assert validate_navigation_url("about:blank") == "about:blank"
    for bad_url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,<h1>x</h1>", "chrome://settings"):
        with pytest.raises(ValueError):
            validate_navigation_url(bad_url)


async def test_browser_launch_refuses_to_fake_missing_chromium(db_session, tmp_path, monkeypatch):
    mgr = BrowserManager()
    profile = BrowserProfile(
        id="profile-no-chromium",
        name="Unavailable Chromium Profile",
        agent_id="worker-3",
        profile_dir=str(tmp_path / "profile-no-chromium"),
        mode="background",
        status="ready",
        current_url="about:blank",
    )
    db_session.add(profile)
    await db_session.commit()
    monkeypatch.setattr(
        "app.browser.manager.detect_playwright_capabilities",
        lambda: {
            "playwright_installed": True,
            "chromium_installed": False,
            "executable_path": None,
            "browser_source": None,
            "engine_mode": "not_implemented",
        },
    )

    with pytest.raises(HTTPException) as unavailable:
        await mgr.launch_profile(db_session, profile, owner="worker-3", mode="background")

    assert unavailable.value.status_code == 501
    assert profile.status == "ready"
    assert profile.lock_owner is None
    assert profile.id not in mgr._active_contexts
    assert not Path(profile.profile_dir).exists()
    sessions = (
        await db_session.execute(select(BrowserSession).where(BrowserSession.profile_id == profile.id))
    ).scalars().all()
    assert sessions == []


async def test_browser_db_file_async_locks_stale_recovery_and_crash(db_session, tmp_path, monkeypatch):
    mgr = BrowserManager()
    _enable_fake_live_browser(monkeypatch, mgr)
    bp = BrowserProfile(
        id="profile-worker3-locktest",
        name="Worker 3 Lock Test Profile",
        agent_id="worker-3",
        profile_dir=str(tmp_path / "profile-worker3-locktest"),
        mode="background",
        status="ready",
        current_url="about:blank",
    )
    db_session.add(bp)
    await db_session.commit()

    # 1. Foreign LIVE file lock blocks launch even when DB status is 'ready'
    pdir = Path(bp.profile_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / ".aatm.lock").write_text(
        json.dumps({"owner": "external-worker", "pid": os.getpid(), "profile_id": bp.id}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="file-locked"):
        await mgr.launch_profile(db_session, bp, owner="worker-3", mode="background")

    # 2. Foreign STALE file lock (dead PID) is automatically recovered
    (pdir / ".aatm.lock").write_text(
        json.dumps({"owner": "dead-worker", "pid": 999999, "profile_id": bp.id}),
        encoding="utf-8",
    )
    launched = await mgr.launch_profile(db_session, bp, owner="worker-3", mode="visible", url="http://localhost:5173")
    assert launched.status == "running"
    assert launched.lock_owner == "worker-3"
    lock_info = mgr.get_lock_status(launched)
    assert lock_info["file_lock_present"] is True
    assert lock_info["file_lock_owner"] == "worker-3"
    assert lock_info["file_lock_alive"] is True

    # 3. DB lock blocks different owner
    with pytest.raises(ValueError, match="locked by 'worker-3'"):
        await mgr.launch_profile(db_session, launched, owner="worker-2", mode="background")

    # 4. Same owner can re-launch cleanly (e.g., mode switch visible -> background)
    relaunched = await mgr.launch_profile(db_session, launched, owner="worker-3", mode="background")
    assert relaunched.mode == "background"
    assert relaunched.status == "running"

    # 5. Crash & self-healing recovery preserves lock file & creates recovered BrowserSession
    recovered = await mgr.simulate_crash_and_recover(db_session, relaunched, recover=True)
    assert recovered.crash_count == 1
    assert recovered.status == "running"
    assert (pdir / ".aatm.lock").exists()
    sessions = (
        await db_session.execute(select(BrowserSession).where(BrowserSession.profile_id == bp.id))
    ).scalars().all()
    assert any(s.status == "crashed" for s in sessions)
    assert any(s.status == "active" and (s.recovery_notes or "").startswith("Auto-recovered") for s in sessions)

    # 6. Clean stop removes lock file and closes active session
    stopped = await mgr.stop_profile(db_session, recovered, actor="worker-3")
    assert stopped.status == "stopped"
    assert stopped.lock_owner is None
    assert not (pdir / ".aatm.lock").exists()

    # 7. Concurrent launch race by two different owners: exactly one succeeds, one fails
    async def attempt(owner: str) -> str:
        async with AsyncSessionLocal() as s:
            prof = await s.get(BrowserProfile, bp.id)
            await mgr.launch_profile(s, prof, owner=owner, mode="background")
            return owner

    results = await asyncio.gather(attempt("worker-3"), attempt("worker-4"), return_exceptions=True)
    successes = [r for r in results if isinstance(r, str)]
    errors = [r for r in results if isinstance(r, ValueError)]
    assert len(successes) == 1
    assert len(errors) == 1

    # Cleanup
    refreshed = await db_session.get(BrowserProfile, bp.id)
    await mgr.stop_profile(db_session, refreshed, actor="pytest")
    await mgr.shutdown_all()


async def test_browser_max_concurrency_limit(db_session, tmp_path, monkeypatch):
    mgr = BrowserManager()
    _enable_fake_live_browser(monkeypatch, mgr)
    settings = get_settings()
    monkeypatch.setattr(settings, "MAX_CONCURRENT_BROWSERS", 1)

    bp1 = BrowserProfile(
        id="profile-cap-1",
        name="Cap Test 1",
        agent_id="worker-3",
        profile_dir=str(tmp_path / "cap1"),
        mode="background",
        status="ready",
    )
    bp2 = BrowserProfile(
        id="profile-cap-2",
        name="Cap Test 2",
        agent_id="worker-4",
        profile_dir=str(tmp_path / "cap2"),
        mode="background",
        status="ready",
    )
    db_session.add_all([bp1, bp2])
    await db_session.commit()

    await mgr.launch_profile(db_session, bp1, owner="worker-3")
    with pytest.raises(ValueError, match="Max concurrent browser limit"):
        await mgr.launch_profile(db_session, bp2, owner="worker-4")

    await mgr.stop_profile(db_session, bp1, actor="pytest")


# ---------------------------------------------------------------------------
# Division 06: Tampermonkey Userscript Policy, Role Scoping & Browser Engines
# ---------------------------------------------------------------------------


def test_tampermonkey_policy_blocks_evasion_harvesting_and_wildcards():
    valid_patterns = ["http://localhost:5173/*", "https://fastapi.tiangolo.com/*"]

    # Prohibited code patterns must all be rejected
    bad_scripts = [
        "solveRecaptcha(); // captcha",
        "await solveTurnstile();",
        "Object.defineProperty(navigator, 'webdriver', { get: () => false });",
        "if (navigator.webdriver) console.log('stealth');",
        "import stealth from 'playwright-stealth';",
        "console.log(document.cookie);",
        'const t = localStorage.getItem("token");',
        "const t = localStorage['auth_token'];",
        "const s = sessionStorage.jwt;",
        "// @grant GM_cookie\nconsole.log('ok');",
        "unsafeWindow.fetch = () => {};",
        "GM_xmlhttpRequest({ method: 'GET', url: 'https://evil.test' });",
        "eval('console.log(1)');",
        "new Function('return 1')();",
        "XMLHttpRequest.prototype.setRequestHeader = function() {};",
        "// ==UserScript==\n// @match *://*/*\n// ==/UserScript==\nconsole.log('ok');",
    ]
    for code in bad_scripts:
        ok, _ = validate_userscript_policy(code, valid_patterns, allowed_roles=["worker"])
        assert ok is False, f"Expected policy rejection for: {code}"

    # Overly broad or non-HTTP(S) @match patterns must be rejected
    for bad_pat in ("<all_urls>", "*://*/*", "https://*/*", "http://*/*", "file:///*", "javascript:alert(1)"):
        ok, _ = validate_userscript_policy("console.log('ok');", [bad_pat], allowed_roles=["worker"])
        assert ok is False, f"Expected @match rejection for: {bad_pat}"

    # Wildcard or empty role-scoping must be rejected
    assert validate_userscript_policy("console.log('ok');", valid_patterns, allowed_roles=["*"])[0] is False
    assert validate_userscript_policy("console.log('ok');", valid_patterns, allowed_roles=[], allowed_agent_ids=[])[0] is False
    assert validate_userscript_policy("console.log('ok');", valid_patterns, allowed_roles=["worker"], allowed_agent_ids=["all"])[0] is False

    # Valid, clean, host-scoped, role-scoped userscript is approved
    clean_code = (
        "// ==UserScript==\n"
        "// @name AATM Safe DOM Inspector\n"
        "// @match http://localhost:*/*\n"
        "// ==/UserScript==\n"
        "console.info('Headings:', document.querySelectorAll('h1').length);"
    )
    ok, status = validate_userscript_policy(
        clean_code,
        ["http://localhost:*/*", "https://*.example.com/docs/*"],
        allowed_roles=["worker", "manager"],
        allowed_agent_ids=["worker-3"],
    )
    assert ok is True
    assert status == "approved"


def test_tampermonkey_url_matching_and_role_scoped_authorization():
    assert match_url_pattern("http://localhost:*/*", "http://localhost:5173/browsers") is True
    assert match_url_pattern("http://localhost:5173/*", "http://localhost:8000/api") is False
    assert match_url_pattern("https://*.example.com/docs/*", "https://api.example.com/docs/v1") is True
    assert match_url_pattern("https://*.example.com/docs/*", "https://evil-example.com/docs/v1") is False

    script = {
        "is_enabled": True,
        "audit_status": "approved",
        "code": "console.info('Safe title:', document.title);",
        "match_patterns": ["http://localhost:*/*", "https://fastapi.tiangolo.com/*"],
        "allowed_roles": ["worker"],
        "allowed_agent_ids": ["worker-2", "worker-3"],
    }

    # Authorized worker + matching URL
    ok, _ = authorize_userscript_execution(
        script, agent_id="worker-3", agent_role="worker", target_url="http://localhost:5173/app"
    )
    assert ok is True

    # Unauthorized role
    ok, msg = authorize_userscript_execution(
        script, agent_id="worker-3", agent_role="guest", target_url="http://localhost:5173/app"
    )
    assert ok is False
    assert "Role" in msg

    # Unauthorized agent ID
    ok, msg = authorize_userscript_execution(
        script, agent_id="worker-1", agent_role="worker", target_url="http://localhost:5173/app"
    )
    assert ok is False
    assert "Agent" in msg

    # Non-matching URL
    ok, msg = authorize_userscript_execution(
        script, agent_id="worker-3", agent_role="worker", target_url="https://github.com/oldman-088/AATM"
    )
    assert ok is False
    assert "@match" in msg

    # Disabled script
    disabled = dict(script, is_enabled=False)
    assert authorize_userscript_execution(
        disabled, agent_id="worker-3", agent_role="worker", target_url="http://localhost:5173/app"
    )[0] is False


async def test_browser_engine_adapters_capabilities_and_policy():
    pw_caps = BROWSER_ENGINE_REGISTRY["playwright"].capabilities()
    real_caps = detect_playwright_capabilities()
    assert pw_caps["chromium_installed"] == real_caps["chromium_installed"]
    assert pw_caps["status"] == ("ready" if real_caps["chromium_installed"] else "not_implemented")
    assert pw_caps["supports_userscripts"] is False
    assert pw_caps["userscripts_mode"] == "storage_only"

    # Prohibited action rejected across engines
    rej = await BROWSER_ENGINE_REGISTRY["playwright"].execute_action("profile-1", "bypass_captcha", {})
    assert rej["status"] == "rejected"

    # A structurally valid workflow cannot report execution without a live browser context.
    automa = BROWSER_ENGINE_REGISTRY["automa"]
    valid_steps = [{"type": "navigate", "url": "https://fastapi.tiangolo.com"}, {"type": "snapshot"}]
    assert automa._validate_workflow_steps(valid_steps, require_executable=True)[0] is True
    with pytest.raises(HTTPException) as unavailable:
        await automa.execute_action("profile-1", "run_workflow", {"steps": valid_steps})
    assert unavailable.value.status_code == 501

    bad_wf = await automa.execute_action(
        "profile-1",
        "run_workflow",
        {"steps": [{"type": "javascript", "url": "https://example.com/", "code": "console.log(document.cookie)"}]},
    )
    assert bad_wf["status"] == "rejected"

    # Selector policy enforced on Playwright & Automa adapters, and raw input values are redacted
    bad_sel = await BROWSER_ENGINE_REGISTRY["playwright"].execute_action(
        "profile-1", "click_element", {"selector": "#g-recaptcha-response"}
    )
    assert bad_sel["status"] == "rejected"

    with pytest.raises(HTTPException) as unavailable:
        await BROWSER_ENGINE_REGISTRY["playwright"].execute_action(
            "profile-1", "fill_input", {"selector": "input[name='query']", "value": "private-query-value-42"}
        )
    assert unavailable.value.status_code == 501
    assert "private-query-value-42" not in str(unavailable.value.detail)

    bad_wf_sel = await automa.execute_action(
        "profile-1",
        "run_workflow",
        {"steps": [{"type": "click", "selector": "iframe[src*='cf-turnstile']"}]},
    )
    assert bad_wf_sel["status"] == "rejected"


async def test_arena_bridge_adapter_delete_session_lifecycle(monkeypatch):
    monkeypatch.setenv("TEST_DELETE_BRIDGE_TOKEN", "bridge-delete-test-token")

    with _MockBridgeServer(expected_bearer="bridge-delete-test-token") as srv:
        srv.sessions["sess-worker-3"] = "acct-alpha"
        adapter = ArenaLocalBridgeAdapter(
            "arena-local-bridge",
            srv.base_url,
            "env:TEST_DELETE_BRIDGE_TOKEN",
            "arena-bridge-default",
        )

        # Invalid session_id format (path traversal blocked before network call)
        bad_id = await adapter.delete_session("../etc/passwd", account_id="acct-alpha")
        assert bad_id["deleted"] is False
        assert bad_id["status"] == "invalid_session_id"
        assert bad_id["bridge_reachable"] is False

        # Cross-account delete rejected with 403 -> status=forbidden
        forbidden = await adapter.delete_session("sess-worker-3", account_id="acct-evil")
        assert forbidden["deleted"] is False
        assert forbidden["status"] == "forbidden"
        assert forbidden["bridge_reachable"] is True
        assert "sess-worker-3" in srv.sessions

        # Authorized delete succeeds -> status=deleted
        deleted = await adapter.delete_session("sess-worker-3", account_id="acct-alpha")
        assert deleted["deleted"] is True
        assert deleted["status"] == "deleted"
        assert deleted["bridge_reachable"] is True
        assert "sess-worker-3" not in srv.sessions

        # Second delete -> status=not_found (idempotent)
        missing = await adapter.delete_session("sess-worker-3", account_id="acct-alpha")
        assert missing["deleted"] is False
        assert missing["status"] == "not_found"
        assert missing["bridge_reachable"] is True

        # Non-env credential_ref rejected by policy
        bad_cred_adapter = ArenaLocalBridgeAdapter(
            "arena-local-bridge", srv.base_url, "raw-inline-token-bad", "arena-bridge-default"
        )
        pol = await bad_cred_adapter.delete_session("sess-worker-3", account_id="acct-alpha")
        assert pol["deleted"] is False
        assert pol["status"] == "policy_error"

    # Server stopped -> offline fallback without exception
    offline = await adapter.delete_session("sess-worker-3", account_id="acct-alpha")
    assert offline["deleted"] is False
    assert offline["status"] == "bridge_offline"
    assert offline["bridge_reachable"] is False


async def test_browser_safe_actions_fail_closed_without_live_playwright(db_session, tmp_path):
    assert validate_selector("#submit-button") == "#submit-button"
    assert validate_selector('input[name="search"]') == 'input[name="search"]'
    for bad_sel in (
        "",
        "   ",
        "#g-recaptcha-response",
        ".cf-turnstile iframe",
        "div[data-hcaptcha-widget-id]",
        "javascript:alert(1)",
        "<script>alert(1)</script>",
    ):
        with pytest.raises(ValueError):
            validate_selector(bad_sel)

    mgr = BrowserManager()
    bp = BrowserProfile(
        id="profile-safe-actions",
        name="Safe Actions Profile",
        agent_id="worker-3",
        profile_dir=str(tmp_path / "profile-safe-actions"),
        mode="background",
        status="ready",
        current_url="about:blank",
    )
    db_session.add(bp)
    await db_session.commit()

    with pytest.raises(ValueError, match="must be running"):
        await mgr.click_element(db_session, bp, "#save-btn", actor="worker-3")
    with pytest.raises(ValueError, match="must be running"):
        await mgr.fill_input(db_session, bp, "input[name='note']", "hello", actor="worker-3")

    bp.status = "running"
    with pytest.raises(HTTPException) as no_click:
        await mgr.click_element(db_session, bp, "#nav-docs", actor="worker-3")
    assert no_click.value.status_code == 501
    with pytest.raises(HTTPException) as no_fill:
        await mgr.fill_input(db_session, bp, "input[name='notes']", "confidential-form-entry", actor="worker-3")
    assert no_fill.value.status_code == 501
    with pytest.raises(HTTPException) as no_snapshot:
        await mgr.capture_snapshot(db_session, bp, actor="worker-3")
    assert no_snapshot.value.status_code == 501
    assert not (await db_session.execute(select(AuditLog).where(AuditLog.resource_id == bp.id))).scalars().all()

    class FakePage:
        def __init__(self) -> None:
            self.url = "https://fastapi.tiangolo.com/tutorial/submitted"
            self.clicked: list[str] = []
            self.filled: list[tuple[str, str]] = []

        async def click(self, selector: str, timeout: int = 5000) -> None:
            self.clicked.append(selector)

        async def fill(self, selector: str, value: str, timeout: int = 5000) -> None:
            self.filled.append((selector, value))

        async def title(self) -> str:
            return "FastAPI Tutorial Live"

        async def screenshot(self, full_page: bool = False, type: str = "png") -> bytes:
            return b"\x89PNG\r\n\x1a\nFAKE_PNG_PAYLOAD"

    class FakeContext:
        def __init__(self, page: FakePage) -> None:
            self.pages = [page]

        async def close(self) -> None:
            return None

    class FakePlaywright:
        async def stop(self) -> None:
            return None

    fake_page = FakePage()
    mgr._active_contexts[bp.id] = (FakePlaywright(), FakeContext(fake_page))
    bp.engine_mode = "playwright"

    live_click = await mgr.click_element(db_session, bp, "button.primary-submit", actor="worker-3")
    assert live_click["ok"] is True and live_click["executed_live"] is True
    assert fake_page.clicked == ["button.primary-submit"]

    secret_value = "live-redacted-field-xyz"
    live_fill = await mgr.fill_input(db_session, bp, "textarea#summary", secret_value, actor="worker-3")
    assert live_fill["ok"] is True and live_fill["executed_live"] is True
    assert live_fill["redacted"] is True
    assert secret_value not in json.dumps(live_fill)
    assert fake_page.filled == [("textarea#summary", secret_value)]

    live_snapshot = await mgr.capture_snapshot(db_session, bp, actor="worker-3", full_page=True)
    assert live_snapshot["ok"] is True and live_snapshot["executed_live"] is True
    assert live_snapshot["format"] == "png"
    assert base64.b64decode(live_snapshot["screenshot_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
    await mgr.stop_profile(db_session, bp, actor="worker-3")



async def test_replace_employee_best_effort_bridge_session_cleanup_online_and_offline(db_session, monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "replace-bridge-token-2026")
    proj = (await db_session.execute(select(Project).limit(1))).scalar_one()

    acct = ProviderAccount(
        id="acct-bridge-replace",
        provider_id="arena-local-bridge",
        label="Bridge Replacement Test Account",
        account_identifier="bridge-replace@arena.local",
        allocation_mode="hybrid",
        max_employees=10,
        credential_ref="env:ARENA_BRIDGE_TOKEN",
        status="active",
    )
    db_session.add(acct)
    await db_session.flush()

    # 1. Online Arena Local Bridge -> DELETE /v1/sessions/{old_session_id} executes and marks DB session closed
    with _MockBridgeServer(expected_bearer="replace-bridge-token-2026") as srv:
        srv.sessions["sess-emp-bridge-online"] = "acct-bridge-replace"
        bridge_prov = await db_session.get(AIProvider, "arena-local-bridge")
        bridge_prov.endpoint_url = srv.base_url

        sess_online = ProviderSession(
            id="sess-emp-bridge-online",
            account_id="acct-bridge-replace",
            agent_id="emp-bridge-online",
            status="active",
        )
        emp_online = Agent(
            id="emp-bridge-online",
            name="Bridge Employee Online",
            role="worker",
            specialty="AI Systems",
            skills=["python", "arena"],
            divisions=["D05"],
            status="working",
            provider_id="arena-local-bridge",
            provider_account_id="acct-bridge-replace",
            provider_session_id="sess-emp-bridge-online",
        )
        task_online = Task(
            id="task-bridge-online",
            project_id=proj.id,
            division_code="D05",
            title="Online bridge replacement handoff",
            status="in_progress",
            assigned_agent_id="emp-bridge-online",
        )
        db_session.add_all([sess_online, emp_online, task_online])
        await db_session.commit()

        result_online = await replacement_service.replace_employee(
            db_session,
            failing_agent_id="emp-bridge-online",
            reason="Repeated timeout",
            actor="manager-0",
        )
        assert result_online["replaced_employee"] == "emp-bridge-online"
        assert "task-bridge-online" in result_online["transferred_tasks"]
        cleanup_online = result_online["bridge_session_cleanup"]
        assert cleanup_online is not None
        assert cleanup_online["deleted"] is True
        assert cleanup_online["status"] == "deleted"
        assert cleanup_online["session_id"] == "sess-emp-bridge-online"
        assert cleanup_online["account_id"] == "acct-bridge-replace"
        assert "sess-emp-bridge-online" not in srv.sessions
        refreshed_sess = await db_session.get(ProviderSession, "sess-emp-bridge-online")
        assert refreshed_sess.status == "closed"

    # 2. Offline Arena Local Bridge -> replacement MUST STILL SUCCEED cleanly (never raises)
    sess_offline = ProviderSession(
        id="sess-emp-bridge-offline",
        account_id="acct-bridge-replace",
        agent_id="emp-bridge-offline",
        status="active",
    )
    emp_offline = Agent(
        id="emp-bridge-offline",
        name="Bridge Employee Offline",
        role="worker",
        specialty="AI Systems",
        skills=["python", "arena"],
        divisions=["D05"],
        status="working",
        provider_id="arena-local-bridge",
        provider_account_id="acct-bridge-replace",
        provider_session_id="sess-emp-bridge-offline",
    )
    task_offline = Task(
        id="task-bridge-offline",
        project_id=proj.id,
        division_code="D05",
        title="Offline bridge replacement handoff",
        status="queued",
        assigned_agent_id="emp-bridge-offline",
    )
    db_session.add_all([sess_offline, emp_offline, task_offline])
    await db_session.commit()

    result_offline = await replacement_service.replace_employee(
        db_session,
        failing_agent_id="emp-bridge-offline",
        reason="Bridge unreachable during failure",
        actor="manager-0",
    )
    assert result_offline["replaced_employee"] == "emp-bridge-offline"
    assert "task-bridge-offline" in result_offline["transferred_tasks"]
    cleanup_offline = result_offline["bridge_session_cleanup"]
    assert cleanup_offline is not None
    assert cleanup_offline["deleted"] is False
    assert cleanup_offline["status"] == "bridge_offline"
    assert cleanup_offline["bridge_reachable"] is False
    emp_offline_db = await db_session.get(Agent, "emp-bridge-offline")
    assert emp_offline_db.status == "offline"

    # 3. Non-bridge employee (arena-agent) -> skips bridge cleanup cleanly
    emp_non_bridge = Agent(
        id="emp-non-bridge",
        name="Non-Bridge Employee",
        role="worker",
        specialty="Docs",
        skills=["markdown"],
        divisions=["D09"],
        status="working",
        provider_id="arena-agent",
        provider_account_id="acct-arena-primary",
        provider_session_id="sess-non-bridge",
    )
    db_session.add(emp_non_bridge)
    await db_session.commit()
    result_non_bridge = await replacement_service.replace_employee(db_session, "emp-non-bridge", reason="Reassigned")
    assert result_non_bridge["bridge_session_cleanup"] is None


@pytest.mark.parametrize("revoke_stage", ["heartbeat", "claim", "report"])
async def test_worker_daemon_halts_cleanly_on_http_401_heartbeat_claim_and_report(
    revoke_stage: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
):
    device_token = "revoked-device-token-do-not-log-999"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/register"):
            return httpx.Response(200, json={"device_id": "node-401", "bound_agent_id": "dev-node-401"})
        if path.endswith("/heartbeat"):
            if revoke_stage == "heartbeat":
                return httpx.Response(401, json={"detail": "Device token revoked"})
            return httpx.Response(200, json={"status": "online"})
        if path.endswith("/claim"):
            if revoke_stage == "claim":
                return httpx.Response(401, json={"detail": "Device token revoked"})
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-401-test",
                        "title": "Test 401 on report",
                        "description": "Execute then report against revoked token",
                        "expected_deliverables": ["report_401.md"],
                    }
                },
            )
        if path.endswith("/report"):
            if revoke_stage == "report":
                return httpx.Response(401, json={"detail": "Device token revoked"})
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://aatm.test") as client:
        daemon = WorkerDaemon(
            WorkerConfig(
                server_url="http://aatm.test",
                device_id="node-401",
                token=device_token,
                workspace_root=tmp_path / f"ws-{revoke_stage}",
                poll_seconds=60.0,  # Large poll interval proves 401 halts immediately without sleeping
            ),
            client=client,
        )
        with caplog.at_level(logging.ERROR, logger="app.runtime.worker_daemon"):
            await daemon.run_forever(max_cycles=5)

    assert daemon.token_revoked is True
    assert daemon.cycles_completed == 1
    assert daemon.stop_reason is not None
    assert "401" in daemon.stop_reason
    assert revoke_stage in daemon.stop_reason

    # Verify explicit token-revocation notice logged AND printed, with ZERO raw token leakage
    out = capsys.readouterr().out
    assert "TOKEN REVOKED (HTTP 401)" in out
    assert revoke_stage in out
    assert device_token not in out
    assert any("TOKEN REVOKED (HTTP 401)" in rec.message and revoke_stage in rec.message for rec in caplog.records)
    assert all(device_token not in rec.message for rec in caplog.records)

    # Confirm no second poll cycle ever happened after HTTP 401
    heartbeat_calls = [p for p in calls if p.endswith("/heartbeat")]
    assert len(heartbeat_calls) == 1


async def test_router_cross_account_session_override_guard_and_health_score_recovery(db_session, monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "router-health-token-2026")

    acct_a = ProviderAccount(
        id="acct-iso-a",
        provider_id="arena-local-bridge",
        label="Account A",
        account_identifier="a@arena.local",
        allocation_mode="hybrid",
        max_employees=5,
        credential_ref="env:ARENA_BRIDGE_TOKEN",
        status="active",
    )
    acct_b = ProviderAccount(
        id="acct-iso-b",
        provider_id="arena-local-bridge",
        label="Account B",
        account_identifier="b@arena.local",
        allocation_mode="hybrid",
        max_employees=5,
        credential_ref="env:ARENA_BRIDGE_TOKEN",
        status="active",
    )
    sess_a = ProviderSession(
        id="sess-iso-a",
        account_id="acct-iso-a",
        agent_id="emp-iso-a",
        status="active",
        health_score=70.0,
    )
    sess_b = ProviderSession(
        id="sess-iso-b",
        account_id="acct-iso-b",
        agent_id="emp-iso-b",
        status="active",
        health_score=100.0,
    )
    emp_a = Agent(
        id="emp-iso-a",
        name="Isolated Employee A",
        role="worker",
        specialty="AI",
        skills=["python"],
        divisions=["D05"],
        status="idle",
        provider_id="arena-local-bridge",
        provider_account_id="acct-iso-a",
        provider_session_id="sess-iso-a",
    )
    db_session.add_all([acct_a, acct_b, sess_a, sess_b, emp_a])
    await db_session.commit()

    router = ProviderRouter()

    # 1. Cross-account explicit session_id override rejected cleanly on route_and_execute AND route_and_stream
    denied_exec = await router.route_and_execute(
        db_session,
        ProviderRequest(prompt="Try hijacking B", agent_id="emp-iso-a", metadata={"session_id": "sess-iso-b"}),
        preferred_provider_id="arena-local-bridge",
    )
    assert denied_exec.success is False
    assert denied_exec.metadata.get("cross_account_denied") is True
    assert "Cross-account" in (denied_exec.error_message or "")

    denied_dry_run = await router.route_and_execute(
        db_session,
        ProviderRequest(
            prompt="Try dry-run hijacking B",
            agent_id="emp-iso-a",
            metadata={"session_id": "sess-iso-b"},
            dry_run=True,
        ),
        preferred_provider_id="arena-local-bridge",
    )
    assert denied_dry_run.success is False
    assert denied_dry_run.metadata.get("cross_account_denied") is True

    denied_stream = [
        c
        async for c in router.route_and_stream(
            db_session,
            ProviderRequest(prompt="Try streaming B", agent_id="emp-iso-a", metadata={"session_id": "sess-iso-b"}),
            preferred_provider_id="arena-local-bridge",
        )
    ]
    assert len(denied_stream) == 1
    assert denied_stream[0].done is True
    assert denied_stream[0].success is False
    assert denied_stream[0].metadata.get("cross_account_denied") is True

    denied_stream_dry = [
        chunk
        async for chunk in router.route_and_stream(
            db_session,
            ProviderRequest(
                prompt="Try dry-run streaming hijacking B",
                agent_id="emp-iso-a",
                metadata={"session_id": "sess-iso-b"},
                dry_run=True,
            ),
            preferred_provider_id="arena-local-bridge",
        )
    ]
    assert len(denied_stream_dry) == 1
    assert denied_stream_dry[0].success is False
    assert denied_stream_dry[0].metadata.get("cross_account_denied") is True

    # 2. Dynamic health_score degradation on repeated HTTP errors and recovery on live success
    for pid in ("arena-agent", "anthropic-claude"):
        prov = await db_session.get(AIProvider, pid)
        if prov:
            prov.is_enabled = False
    await db_session.commit()

    with _MockBridgeServer(expected_bearer="wrong-server-token-forces-401") as failing_srv:
        bridge_prov = await db_session.get(AIProvider, "arena-local-bridge")
        bridge_prov.endpoint_url = failing_srv.base_url
        await db_session.commit()

        # Two HTTP 401 failures drop health_score from 70 -> 55 -> 40 (<50 => degraded)
        r1 = await router.route_and_execute(
            db_session,
            ProviderRequest(prompt="Fail 1", agent_id="emp-iso-a", dry_run=False, allow_diagnostic_fallback=False),
            preferred_provider_id="arena-local-bridge",
        )
        r2 = await router.route_and_execute(
            db_session,
            ProviderRequest(prompt="Fail 2", agent_id="emp-iso-a", dry_run=False, allow_diagnostic_fallback=False),
            preferred_provider_id="arena-local-bridge",
        )
        assert r1.success is False and r2.success is False
        s_degraded = await db_session.get(ProviderSession, "sess-iso-a")
        assert s_degraded.health_score == 40.0
        assert s_degraded.status == "degraded"
        assert s_degraded.error_count == 2

    with _MockBridgeServer(expected_bearer="router-health-token-2026") as healthy_srv:
        bridge_prov = await db_session.get(AIProvider, "arena-local-bridge")
        bridge_prov.endpoint_url = healthy_srv.base_url
        await db_session.commit()

        # Two healthy turns recover health_score from 40 -> 50 -> 60 (>=60 => restored active!)
        ok1 = await router.route_and_execute(
            db_session,
            ProviderRequest(prompt="Recover 1", agent_id="emp-iso-a", dry_run=False),
            preferred_provider_id="arena-local-bridge",
        )
        ok2 = await router.route_and_execute(
            db_session,
            ProviderRequest(prompt="Recover 2", agent_id="emp-iso-a", dry_run=False),
            preferred_provider_id="arena-local-bridge",
        )
        assert ok1.success is True and ok2.success is True
        s_recovered = await db_session.get(ProviderSession, "sess-iso-a")
        assert s_recovered.health_score == 60.0
        assert s_recovered.status == "active"
        assert s_recovered.last_error is None


async def test_automa_workflow_step_execution_redaction_and_policy_rejection(db_session, tmp_path, monkeypatch):
    async def allow_public_url(url: str) -> str:
        return url

    monkeypatch.setattr("app.browser.manager.validate_public_navigation_url", allow_public_url)
    mgr = BrowserManager()
    bp = BrowserProfile(
        id="profile-automa-runner",
        name="Automa Step Runner Profile",
        agent_id="worker-3",
        profile_dir=str(tmp_path / "profile-automa-runner"),
        mode="background",
        status="running",
        current_url="about:blank",
    )
    db_session.add(bp)
    await db_session.commit()
    automa = AutomaWorkflowAdapter()

    rejected = await automa.execute_workflow(
        db_session,
        bp,
        [
            {"type": "navigate", "url": "https://fastapi.tiangolo.com/"},
            {"type": "click_element", "selector": "#g-recaptcha-response"},
        ],
        manager=mgr,
        actor="worker-3",
    )
    assert rejected["status"] == "rejected"
    assert rejected["steps_completed"] == 0
    assert rejected["trace"] == []
    assert bp.current_url == "about:blank"

    secret_field_text = "automa-redacted-entry-554433"
    steps = [
        {"type": "navigate", "url": "https://fastapi.tiangolo.com/tutorial/"},
        {"type": "fill_input", "selector": "input[name='q']", "value": secret_field_text},
        {"type": "click_element", "selector": "button[type='submit']"},
        {"type": "extract_text", "selector": "#summary"},
        {"type": "wait", "ms": 1},
        {"type": "capture_snapshot"},
    ]
    with pytest.raises(HTTPException) as unavailable:
        await automa.execute_workflow(db_session, bp, steps, manager=mgr, actor="worker-3")
    assert unavailable.value.status_code == 501
    assert bp.current_url == "about:blank"
    assert secret_field_text not in json.dumps(
        (await db_session.execute(select(AuditLog).where(AuditLog.resource_id == bp.id))).scalars().all(),
        default=str,
    )

    class FakeLocator:
        @property
        def first(self):
            return self

        async def inner_text(self, timeout: int = 5_000) -> str:
            return "Build Passed! sessionid=never_expose_441"

    class FakePage:
        def __init__(self) -> None:
            self.url = "about:blank"
            self.calls: list[tuple[str, str]] = []

        async def goto(self, url: str, timeout: int = 10_000) -> None:
            self.url = url
            self.calls.append(("goto", url))

        async def fill(self, selector: str, value: str, timeout: int = 5_000) -> None:
            self.calls.append(("fill", selector))

        async def click(self, selector: str, timeout: int = 5_000) -> None:
            self.calls.append(("click", selector))

        async def title(self) -> str:
            return "Live Automa Page"

        def locator(self, selector: str) -> FakeLocator:
            self.calls.append(("extract_text", selector))
            return FakeLocator()

        async def screenshot(self, full_page: bool = False, type: str = "png") -> bytes:
            self.calls.append(("screenshot", "page"))
            return b"\x89PNG\r\n\x1a\nLIVE_AUTOMA"

    class FakeContext:
        def __init__(self, page: FakePage) -> None:
            self.pages = [page]

        async def close(self) -> None:
            return None

    class FakePlaywright:
        async def stop(self) -> None:
            return None

    fake_page = FakePage()
    mgr._active_contexts[bp.id] = (FakePlaywright(), FakeContext(fake_page))
    res = await automa.execute_workflow(db_session, bp, steps, manager=mgr, actor="worker-3")
    assert res["status"] == "completed"
    assert res["steps_completed"] == len(steps)
    assert res["trace"][1]["redacted"] is True
    assert res["trace"][1]["value_length"] == len(secret_field_text)
    assert "never_expose_441" not in json.dumps(res)
    assert secret_field_text not in json.dumps(res)
    assert ("fill", "input[name='q']") in fake_page.calls
    assert ("click", "button[type='submit']") in fake_page.calls

    direct = await automa.execute_action(
        bp.id,
        "run_workflow",
        {
            "db": db_session,
            "profile": bp,
            "manager": mgr,
            "actor": "worker-3",
            "steps": [{"type": "click_element", "selector": "#go"}],
        },
    )
    assert direct["status"] == "completed"
    assert direct["steps_completed"] == 1
    assert fake_page.calls[-1] == ("click", "#go")

    await mgr.stop_profile(db_session, bp, actor="worker-3")



async def test_browser_manager_userscripts_remain_storage_only_even_with_live_context(db_session, tmp_path):
    mgr = BrowserManager()
    bp = BrowserProfile(
        id="profile-userscript-harness",
        name="Userscript Harness Profile",
        agent_id="worker-3",
        profile_dir=str(tmp_path / "profile-userscript-harness"),
        mode="background",
        status="running",
        current_url="https://example.com/dashboard",
    )
    db_session.add(bp)
    await db_session.commit()
    approved_script = {
        "id": "us-safe-inspector",
        "name": "Safe Inspector",
        "is_enabled": True,
        "audit_status": "approved",
        "code": "return document.title;",
        "match_patterns": ["https://example.com/*"],
        "allowed_roles": ["worker"],
        "allowed_agent_ids": ["worker-3"],
    }

    with pytest.raises(HTTPException) as unavailable:
        await mgr.run_userscript(
            db_session, bp, approved_script, agent_id="worker-3", agent_role="worker"
        )
    assert unavailable.value.status_code == 501
    assert "storage only" in unavailable.value.detail.lower()

    class EvalPage:
        def __init__(self) -> None:
            self.evaluated: list[str] = []

        async def evaluate(self, script: str) -> str:
            self.evaluated.append(script)
            return "should never execute"

    class EvalContext:
        def __init__(self, page: EvalPage) -> None:
            self.pages = [page]

        async def close(self) -> None:
            return None

    class FakePlaywright:
        async def stop(self) -> None:
            return None

    eval_page = EvalPage()
    mgr._active_contexts[bp.id] = (FakePlaywright(), EvalContext(eval_page))
    with pytest.raises(HTTPException) as live_unavailable:
        await mgr.run_userscript(
            db_session, bp, approved_script, agent_id="worker-3", agent_role="worker"
        )
    assert live_unavailable.value.status_code == 501
    assert eval_page.evaluated == []

    with pytest.raises(HTTPException) as adapter_unavailable:
        await BROWSER_ENGINE_REGISTRY["playwright"].execute_action(bp.id, "run_userscript", {})
    assert adapter_unavailable.value.status_code == 501
    executions = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.resource_id == bp.id, AuditLog.action == "browser.userscript.executed")
        )
    ).scalars().all()
    assert executions == []



async def test_replace_employee_stops_running_browser_profiles_and_releases_locks(db_session, tmp_path, monkeypatch):
    _enable_fake_live_browser(monkeypatch, browser_manager)
    bp_dir = tmp_path / "profile-replace-lock"
    bp = BrowserProfile(
        id="profile-replace-lock",
        name="Replace Browser Lock Test",
        agent_id="emp-browser-holder",
        profile_dir=str(bp_dir),
        mode="background",
        status="ready",
        current_url="about:blank",
    )
    emp = Agent(
        id="emp-browser-holder",
        name="Browser Holding Employee",
        role="worker",
        specialty="Browser QA",
        skills=["playwright", "python"],
        divisions=["D06"],
        status="working",
        provider_id="arena-agent",
        browser_profile_id="profile-replace-lock",
    )
    db_session.add_all([bp, emp])
    await db_session.commit()

    await browser_manager.launch_profile(db_session, bp, owner="emp-browser-holder", mode="background")
    lock_file = bp_dir / ".aatm.lock"
    assert bp.status == "running"
    assert lock_file.exists()

    result = await replacement_service.replace_employee(
        db_session,
        failing_agent_id="emp-browser-holder",
        reason="Employee unresponsive with locked browser",
        actor="manager-0",
    )
    assert "profile-replace-lock" in result["stopped_browsers"]

    refreshed_bp = await db_session.get(BrowserProfile, "profile-replace-lock")
    assert refreshed_bp.status == "stopped"
    assert refreshed_bp.lock_owner is None
    assert not lock_file.exists()


async def test_worker_daemon_inflight_keepalive_and_step_trace_report(tmp_path: Path):
    heartbeat_count = 0
    model_turns = 0
    reported_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal heartbeat_count, model_turns
        path = request.url.path
        if path.endswith("/heartbeat"):
            heartbeat_count += 1
            return httpx.Response(200, json={"status": "online"})
        if path.endswith("/claim"):
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-keepalive-1",
                        "title": "Long running task",
                        "description": "Verify in-flight keepalive heartbeats & step trace",
                        "expected_deliverables": ["keepalive_deliverable.md"],
                        "employee": {"permissions": ["workspace_exec"]},
                    }
                },
            )
        if path.endswith("/model"):
            model_turns += 1
            content = (
                '{"action":"write","path":"keepalive_deliverable.md","content":"Completed\\n"}'
                if model_turns == 1
                else '{"action":"done","summary":"Keepalive task complete.","deliverables":["keepalive_deliverable.md"]}'
            )
            return httpx.Response(200, json={"content": content})
        if path.endswith("/progress"):
            return httpx.Response(200, json={"status": "recorded"})
        if path.endswith("/report"):
            reported_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://aatm.test") as client:
        daemon = WorkerDaemon(
            WorkerConfig(
                server_url="http://aatm.test",
                device_id="node-keepalive",
                token="keepalive-valid-token-2026",
                workspace_root=tmp_path / "ws-keepalive",
                poll_seconds=5.0,
                keepalive_seconds=0.02,
            ),
            client=client,
        )

        orig_run_steps = daemon._run_task_steps

        async def slow_run_steps(task: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
            await asyncio.sleep(0.08)
            return await orig_run_steps(task, trace)

        daemon._run_task_steps = slow_run_steps  # type: ignore[method-assign]
        executed = await daemon.run_once()

    assert executed is not None
    assert daemon.token_revoked is False
    # 1 initial heartbeat from run_once + >= 2 in-flight keepalive heartbeats during slow_run_steps
    assert heartbeat_count >= 3
    assert len(reported_payloads) == 1
    rep = reported_payloads[0]
    assert rep["success"] is True
    assert rep["keepalive_heartbeats"] >= 2
    assert rep["total_elapsed_ms"] >= 50.0
    assert len(rep["trace"]) >= 2
    assert rep["trace"][0]["step"] == "agent_iteration_1"
    assert rep["trace"][0]["action"] == "write"
    assert rep["trace"][0]["exit_code"] == 0


async def test_worker_daemon_mid_task_keepalive_401_halts_immediately(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
):
    device_token = "midtask-revoked-token-never-log-777"
    heartbeat_calls = 0
    report_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal heartbeat_calls, report_calls
        path = request.url.path
        if path.endswith("/register"):
            return httpx.Response(200, json={"device_id": "node-mid401", "bound_agent_id": "dev-node-mid401"})
        if path.endswith("/heartbeat"):
            heartbeat_calls += 1
            # Initial cycle heartbeat succeeds; first in-flight keepalive heartbeat receives HTTP 401!
            if heartbeat_calls >= 2:
                return httpx.Response(401, json={"detail": "Device token revoked mid-task"})
            return httpx.Response(200, json={"status": "online"})
        if path.endswith("/claim"):
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-mid401",
                        "title": "Slow task interrupted by token revocation",
                        "expected_deliverables": ["never_finished.md"],
                    }
                },
            )
        if path.endswith("/report"):
            report_calls += 1
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://aatm.test") as client:
        daemon = WorkerDaemon(
            WorkerConfig(
                server_url="http://aatm.test",
                device_id="node-mid401",
                token=device_token,
                workspace_root=tmp_path / "ws-mid401",
                poll_seconds=60.0,
                keepalive_seconds=0.02,
            ),
            client=client,
        )

        async def very_slow_steps(task: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
            await asyncio.sleep(5.0)
            return {"success": True, "summary": "should be cancelled", "artifacts": [], "trace": trace}

        daemon._run_task_steps = very_slow_steps  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR, logger="app.runtime.worker_daemon"):
            await daemon.run_forever(max_cycles=5)

    assert daemon.token_revoked is True
    assert daemon.cycles_completed == 1
    assert heartbeat_calls == 2
    assert report_calls == 0

    out = capsys.readouterr().out
    assert "TOKEN REVOKED (HTTP 401)" in out
    assert device_token not in out
    assert any("TOKEN REVOKED (HTTP 401)" in rec.message for rec in caplog.records)
    assert all(device_token not in rec.message for rec in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Round 3: extract_text + BrowserUse/OpenBrowser + Skill Gaps + Replacement + Recovery
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_extract_text_policy_redaction_and_live_context_only(
    db_session, tmp_path: Path
) -> None:
    bearer_tok = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + "eyJzdWIiOiJ1c2VyLTEifQ." + "c2lnbmF0dXJlMTIzNDU2"
    raw_dom = (
        f"Dashboard Ready. Authorization: Bearer {bearer_tok} "
        "cookie: sessionid=sess_secret_val_9981; csrftoken=csrf_val_777 "
        "access_token=tok_secret_value_abc12345"
    )
    masked, redacted_count = redact_extracted_text(raw_dom)
    assert "Dashboard Ready." in masked
    assert "<redacted>" in masked
    assert redacted_count >= 3
    assert bearer_tok not in masked
    assert "sess_secret_val_9981" not in masked
    assert "tok_secret_value_abc12345" not in masked

    profile = BrowserProfile(
        id="bp-extract-test",
        name="Extract Profile",
        agent_id="worker-3",
        profile_dir=str(tmp_path / "bp-extract"),
        mode="background",
        status="ready",
        current_url="about:blank",
    )
    db_session.add(profile)
    await db_session.commit()
    mgr = BrowserManager()

    with pytest.raises(ValueError, match="must be running"):
        await mgr.extract_text(db_session, profile, "main")
    profile.status = "running"
    with pytest.raises(ValueError, match="Policy violation"):
        await mgr.extract_text(db_session, profile, "#cf-turnstile")
    with pytest.raises(HTTPException) as unavailable:
        await mgr.extract_text(db_session, profile, "main", max_chars=250, actor="worker-3")
    assert unavailable.value.status_code == 501

    class FakeLocator:
        def __init__(self, text_val: str) -> None:
            self._text_val = text_val

        @property
        def first(self) -> "FakeLocator":
            return self

        async def inner_text(self, timeout: int = 5_000) -> str:
            return self._text_val

    class FakeExtractPage:
        def __init__(self, text_val: str) -> None:
            self.url = "https://example.com/status"
            self._text_val = text_val

        async def title(self) -> str:
            return "Status Page"

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(self._text_val)

    class FakeExtractContext:
        def __init__(self, page: FakeExtractPage) -> None:
            self.pages = [page]

        async def close(self) -> None:
            return None

    class FakeExtractPlaywright:
        async def stop(self) -> None:
            return None

    profile.engine_mode = "playwright"
    mgr._active_contexts[profile.id] = (
        FakeExtractPlaywright(),
        FakeExtractContext(FakeExtractPage(raw_dom + " " + ("Z" * 120))),
    )
    live_res = await mgr.extract_text(db_session, profile, "article.status", max_chars=90, actor="worker-3")
    assert live_res["ok"] is True
    assert live_res["engine_mode"] == "playwright"
    assert live_res["truncated"] is True
    assert len(live_res["text"]) == 90
    assert live_res["redacted_count"] >= 3
    assert "sess_secret_val_9981" not in live_res["text"]
    assert bearer_tok not in live_res["text"]

    audit_res = await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "browser.extract_text",
            AuditLog.resource_id == profile.id,
        )
    )
    audits = list(audit_res.scalars().all())
    assert len(audits) == 1
    for row in audits:
        dumped = json.dumps(row.details or {})
        assert "sess_secret_val_9981" not in dumped
        assert bearer_tok not in dumped
        assert "text" not in (row.details or {})

    await mgr.stop_profile(db_session, profile, actor="worker-3")



@pytest.mark.asyncio
async def test_browser_use_and_openbrowser_workflows_run_against_live_context_mocks(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = BrowserProfile(
        id="bp-real-engines",
        name="Real Engines Profile",
        agent_id="worker-3",
        profile_dir=str(tmp_path / "bp-engines"),
        mode="background",
        status="running",
    )
    db_session.add(profile)
    await db_session.commit()

    mgr = BrowserManager()
    monkeypatch.setattr(
        "app.browser.manager.detect_playwright_capabilities",
        lambda: {
            "playwright_installed": True,
            "chromium_installed": True,
            "executable_path": "/mock/chromium",
            "browser_source": "test-harness",
            "engine_mode": "playwright",
        },
    )
    monkeypatch.setattr(
        "app.browser.engines.detect_playwright_capabilities",
        lambda: {
            "playwright_installed": True,
            "chromium_installed": True,
            "executable_path": "/mock/chromium",
            "browser_source": "test-harness",
            "engine_mode": "playwright",
        },
    )

    async def allow_public_url(url: str) -> str:
        return url

    monkeypatch.setattr("app.browser.manager.validate_public_navigation_url", allow_public_url)

    class _FakeLoc:
        def __init__(self, selector: str, log: list[tuple[str, str]]) -> None:
            self._selector = selector
            self._log = log

        @property
        def first(self) -> "_FakeLoc":
            return self

        async def click(self, timeout: int = 5000) -> None:
            self._log.append(("click", self._selector))

        async def fill(self, value: str, timeout: int = 5000) -> None:
            self._log.append(("fill", self._selector))

        async def inner_text(self, timeout: int = 5000) -> str:
            self._log.append(("extract_text", self._selector))
            return "Build Passed! sessionid=never_expose_441"

    class _FakePage:
        def __init__(self) -> None:
            self.url = "about:blank"
            self.calls: list[tuple[str, str]] = []

        async def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 20000) -> None:
            self.url = url
            self.calls.append(("goto", url))

        async def click(self, selector: str, timeout: int = 5000) -> None:
            self.calls.append(("click", selector))

        async def fill(self, selector: str, value: str, timeout: int = 5000) -> None:
            self.calls.append(("fill", selector))

        async def title(self) -> str:
            return "Build Portal"

        def locator(self, selector: str) -> _FakeLoc:
            return _FakeLoc(selector, self.calls)

        async def screenshot(self, full_page: bool = False, type: str = "png") -> bytes:
            self.calls.append(("screenshot", "page"))
            return b"\x89PNG\r\n\x1a\nfake-png"

    class _FakeCtx:
        def __init__(self, page: _FakePage) -> None:
            self.pages = [page]

        async def close(self) -> None:
            return None

    class _FakePw:
        async def stop(self) -> None:
            return None

    for engine_key in ("browser_use", "openbrowser"):
        adapter = BROWSER_ENGINE_REGISTRY[engine_key]
        caps = adapter.capabilities()
        assert caps["supports_step_execution"] is True
        assert caps["status"] == "ready"
        assert "extract_text" in caps["supported_steps"]

        fake_page = _FakePage()
        mgr._active_contexts[profile.id] = (_FakePw(), _FakeCtx(fake_page))

        # 1. Structured multi-step workflow via execute_workflow
        wf_steps = [
            {"type": "navigate", "url": "https://example.com/ci"},
            {"type": "fill_input", "selector": "#search", "value": "release-v3"},
            {"type": "click_element", "selector": "#run-btn"},
            {"type": "extract_text", "selector": "#summary", "max_chars": 200},
            {"type": "capture_snapshot", "label": f"{engine_key}-snap"},
            {"type": "wait", "ms": 1},
        ]
        wf_res = await adapter.execute_workflow(db_session, profile, wf_steps, manager=mgr, actor="worker-3")
        assert wf_res["status"] == "completed"
        assert wf_res["engine"] == engine_key
        assert wf_res["steps_completed"] == 6
        extract_step = wf_res["trace"][3]
        assert extract_step["type"] == "extract_text"
        assert "Build Passed!" in extract_step["text"]
        assert "never_expose_441" not in extract_step["text"]
        assert ("goto", "https://example.com/ci") in fake_page.calls
        assert ("fill", "#search") in fake_page.calls
        assert ("click", "#run-btn") in fake_page.calls
        assert ("extract_text", "#summary") in fake_page.calls

        # 2. Direct execute_action with db+profile routes through BrowserManager
        act_res = await adapter.execute_action(
            profile.id,
            "extract_text",
            {"db": db_session, "profile": profile, "manager": mgr, "selector": "#summary", "actor": "worker-3"},
        )
        assert act_res["status"] == "completed"
        assert act_res["engine"] == engine_key
        assert act_res["steps_completed"] == 1
        assert "Build Passed!" in act_res["trace"][0]["text"]

        # 3. Fail-closed CAPTCHA selector rejection (zero steps executed)
        bad_wf = await adapter.execute_workflow(
            db_session,
            profile,
            [
                {"type": "navigate", "url": "https://example.com/ci"},
                {"type": "extract_text", "selector": "#g-recaptcha-response"},
            ],
            manager=mgr,
            actor="worker-3",
        )
        assert bad_wf["status"] == "rejected"
        assert bad_wf["steps_completed"] == 0

    await mgr.stop_profile(db_session, profile, actor="worker-3")


@pytest.mark.asyncio
async def test_dynamic_team_skill_gap_analyzer_and_specialist_provisioning(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")

    required = ["indexing", "migrations", "browser_use", "quantum_ml"]
    gap_before = await analyze_team_skill_gaps(db_session, required, divisions=["D02", "D06"])
    assert gap_before["active_employee_count"] >= 1
    for skill in ("indexing", "migrations", "browser_use", "quantum_ml"):
        assert skill in gap_before["missing_skills"]

    rec_ids = [r["template_id"] for r in gap_before["recommended_templates"]]
    assert "database_engineer" in rec_ids
    assert "browser_automation_engineer" in rec_ids
    assert "quantum_ml" in gap_before["unmatched_skills"]

    provisioned = await provision_missing_specialists(
        db_session,
        required,
        divisions=["D02", "D06"],
        provider_id="arena-agent",
        actor="manager-0",
    )
    assert provisioned["provisioned_count"] >= 3
    assert provisioned["after"]["missing_skills"] == []
    assert provisioned["after"]["missing_divisions"] == []
    assert provisioned["after"]["coverage_ratio"] == 1.0

    # Verify provisioned agents have active ProviderSessions in DB
    for emp_info in provisioned["provisioned_employees"]:
        emp = await db_session.get(Agent, emp_info["id"])
        assert emp is not None
        assert emp.provider_session_id is not None
        sess = await db_session.get(ProviderSession, emp.provider_session_id)
        assert sess is not None
        assert sess.status == "active"


@pytest.mark.asyncio
async def test_replace_employee_allocates_replacement_session_and_cleans_stale_worktree(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.git_workflows.manager import git_workflow_manager
    from app.orchestration.replacement import ReplacementAndEmergencyService

    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")

    # Create a failing specialist and a matching replacement candidate with NO ProviderSession
    failing = Agent(
        id="emp-fail-r3",
        name="Failing R3 Specialist",
        role="worker",
        specialty="Rust Kernel Specialist",
        divisions=["D99"],
        skills=["rust_kernel_r3", "ebpf_r3"],
        provider_id="arena-agent",
        status="error",
        is_enabled=True,
        workspace_path=str(tmp_path / "workspaces" / "emp-fail-r3"),
    )
    replacement_cand = Agent(
        id="emp-rep-r3",
        name="Backup R3 Specialist",
        role="worker",
        specialty="Rust Kernel Backup",
        divisions=["D99"],
        skills=["rust_kernel_r3", "ebpf_r3"],
        provider_id="arena-agent",
        provider_account_id=None,
        provider_session_id=None,
        status="idle",
        is_enabled=True,
        workspace_path=str(tmp_path / "workspaces" / "emp-rep-r3"),
    )
    db_session.add_all([failing, replacement_cand])
    await db_session.commit()

    worktree_calls: list[str] = []

    def _fake_remove_worktree(agent_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        worktree_calls.append(agent_id)
        return {"ok": True, "removed": True, "agent_id": agent_id}

    monkeypatch.setattr(git_workflow_manager, "remove_agent_worktree", _fake_remove_worktree)

    svc = ReplacementAndEmergencyService()
    res = await svc.replace_employee(db_session, failing.id, reason="heartbeat timeout", actor="manager-0")

    assert res["replacement_employee"] == replacement_cand.id
    assert res["replacement_session_allocated"] is not None
    assert res["replacement_session_id"] is not None
    assert worktree_calls == [failing.id]
    assert res["worktree_cleanup"]["removed"] is True

    # Confirm replacement agent now owns a real active ProviderSession in DB
    refreshed_rep = await db_session.get(Agent, replacement_cand.id)
    assert refreshed_rep is not None
    assert refreshed_rep.provider_session_id == res["replacement_session_id"]
    rep_sess = await db_session.get(ProviderSession, refreshed_rep.provider_session_id)
    assert rep_sess is not None
    assert rep_sess.status == "active"

    # Confirm AuditLog recorded replacement_session_allocated and worktree_cleanup
    audit_res = await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "employee.replaced",
            AuditLog.resource_id == failing.id,
        )
    )
    audit = audit_res.scalar_one()
    assert audit.details["replacement_session_id"] == res["replacement_session_id"]
    assert audit.details["worktree_cleanup"]["removed"] is True

    # Verify worktree cleanup failure is best-effort and NEVER aborts replacement
    failing2 = Agent(
        id="emp-fail-r3-b",
        name="Failing R3 B",
        role="worker",
        specialty="Rust Kernel B",
        divisions=["D99"],
        skills=["rust_kernel_r3", "ebpf_r3"],
        provider_id="arena-agent",
        status="error",
        is_enabled=True,
        workspace_path=str(tmp_path / "workspaces" / "emp-fail-r3-b"),
    )
    db_session.add(failing2)
    await db_session.commit()

    def _boom_worktree(agent_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("git binary missing")

    monkeypatch.setattr(git_workflow_manager, "remove_agent_worktree", _boom_worktree)
    res2 = await svc.replace_employee(db_session, failing2.id, reason="crash", actor="manager-0")
    assert res2["replacement_employee"] == replacement_cand.id
    # Replacement already has an active session -> not re-allocated
    assert res2["replacement_session_allocated"] is None
    assert res2["worktree_cleanup"]["ok"] is False
    assert "RuntimeError" in res2["worktree_cleanup"]["error"]


@pytest.mark.asyncio
async def test_provider_router_recover_degraded_sessions_online_and_offline(db_session, monkeypatch) -> None:
    monkeypatch.setenv("RECOVERY_BRIDGE_TOKEN", "session-recovery-token")
    with _MockBridgeServer(expected_bearer="session-recovery-token") as srv:
        p_bridge = await db_session.get(AIProvider, "arena-local-bridge")
        assert p_bridge is not None
        p_bridge.endpoint_url = srv.base_url
        p_bridge.credential_ref = "env:RECOVERY_BRIDGE_TOKEN"
        p_bridge.is_enabled = True

        acct_bridge = ProviderAccount(
            id="acct-rec-bridge",
            provider_id="arena-local-bridge",
            label="Bridge Recovery Account",
            account_identifier="bridge-rec@local",
            status="active",
        )
        acct_agent = ProviderAccount(
            id="acct-rec-agent",
            provider_id="arena-agent",
            label="Agent Recovery Account",
            account_identifier="agent-rec@local",
            status="active",
        )
        db_session.add_all([acct_bridge, acct_agent])

        sess_deg_bridge = ProviderSession(
            id="sess-deg-bridge-1",
            account_id=acct_bridge.id,
            agent_id="worker-3",
            status="degraded",
            health_score=25.0,
            error_count=4,
            last_error="HTTP 502",
        )
        sess_deg_agent = ProviderSession(
            id="sess-deg-agent-1",
            account_id=acct_agent.id,
            agent_id="worker-2",
            status="active",
            health_score=42.0,
            error_count=2,
            last_error="Timeout",
        )
        sess_healthy = ProviderSession(
            id="sess-healthy-keep",
            account_id=acct_bridge.id,
            agent_id="worker-1",
            status="active",
            health_score=95.0,
            error_count=0,
        )
        db_session.add_all([sess_deg_bridge, sess_deg_agent, sess_healthy])
        await db_session.commit()

        # Seed bridge-side session so DELETE /v1/sessions/{id} deletes it cleanly
        srv.sessions[sess_deg_bridge.id] = acct_bridge.id

        router = ProviderRouter()
        report = await router.recover_degraded_sessions(db_session, actor="ops-admin")
        assert report["recovered_count"] == 2
        recovered_ids = {s["session_id"] for s in report["sessions"]}
        assert recovered_ids == {sess_deg_bridge.id, sess_deg_agent.id}
        assert sess_deg_bridge.id not in srv.sessions

        bridge_entry = next(s for s in report["sessions"] if s["session_id"] == sess_deg_bridge.id)
        assert bridge_entry["bridge_reset"]["deleted"] is True

        # Confirm DB rows updated accurately & healthy session untouched
        for sid in (sess_deg_bridge.id, sess_deg_agent.id):
            row = await db_session.get(ProviderSession, sid)
            assert row is not None
            assert row.health_score == 100.0
            assert row.status == "active"
            assert row.error_count == 0
            assert row.last_error is None

        keep_row = await db_session.get(ProviderSession, sess_healthy.id)
        assert keep_row is not None
        assert keep_row.health_score == 95.0

        # Confirm AuditLog entries written
        audit_res = await db_session.execute(
            select(AuditLog).where(AuditLog.action == "provider.session_recovered")
        )
        audits = list(audit_res.scalars().all())
        assert {a.resource_id for a in audits} == {sess_deg_bridge.id, sess_deg_agent.id}

        # Second call with no degraded sessions is a clean no-op
        noop = await router.recover_degraded_sessions(db_session, actor="ops-admin")
        assert noop["recovered_count"] == 0

    # Offline bridge recovery still restores DB session cleanly without crashing
    sess_deg_offline = ProviderSession(
        id="sess-deg-offline",
        account_id="acct-rec-bridge",
        agent_id="worker-3",
        status="degraded",
        health_score=10.0,
        error_count=5,
        last_error="bridge down",
    )
    db_session.add(sess_deg_offline)
    await db_session.commit()

    p_bridge_off = await db_session.get(AIProvider, "arena-local-bridge")
    assert p_bridge_off is not None
    p_bridge_off.endpoint_url = "http://127.0.0.1:1"
    await db_session.commit()

    off_report = await ProviderRouter().recover_degraded_sessions(db_session, actor="ops-admin")
    assert off_report["recovered_count"] == 1
    assert off_report["sessions"][0]["bridge_reset"]["status"] == "bridge_offline"
    refreshed_off = await db_session.get(ProviderSession, "sess-deg-offline")
    assert refreshed_off is not None
    assert refreshed_off.status == "active"
    assert refreshed_off.health_score == 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Round 4: OS/Container Sandbox Isolation, Env Scrubbing & Scheduler Recovery
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sandbox_env_scrubbing_and_subprocess_isolation_executor_and_daemon(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.runtime.executor import AgentRuntimeExecutor
    from app.runtime.sandbox import (
        SandboxExecutionPolicy,
        WorkspaceSandboxError,
        build_resource_preexec,
        build_sanitized_env,
    )
    from app.runtime.worker_daemon import WorkerConfig, WorkerDaemon

    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")

    # Inject sensitive host variables (constructed dynamically to avoid literal secret strings)
    sensitive_vars = {
        "SECRET_KEY": "jwt-" + "master-" + "val-001",
        "ANTHROPIC_API_KEY": "ant-" + "live-" + "val-002",
        "ARENA_BRIDGE_TOKEN": "bridge-" + "tok-" + "val-003",
        "AATM_GITHUB_WEBHOOK_SECRET": "ghwh-" + "sec-" + "val-004",
        "AATM_DEVICE_TOKEN": "dev-" + "tok-" + "val-005",
        "CUSTOM_SERVICE_TOKEN": "svc-" + "tok-" + "val-006",
        "DB_PASSWORD": "db-" + "pw-" + "val-007",
        "APP_PRIVATE_KEY": "priv-" + "key-" + "val-008",
    }
    for k, v in sensitive_vars.items():
        monkeypatch.setenv(k, v)

    # 1. Unit check: build_sanitized_env strips host AND extra secrets while keeping allowlisted vars
    sanitized = build_sanitized_env(
        extra={
            "SAFE_CUSTOM_TAG": "allowed-tag",
            "EXTRA_API_KEY": "must-be-dropped",
            "EXTRA_SECRET": "must-be-dropped",
            "EXTRA_PASSWORD": "must-be-dropped",
            "EXTRA_TOKEN": "must-be-dropped",
        }
    )
    assert sanitized.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert sanitized.get("SAFE_CUSTOM_TAG") == "allowed-tag"
    for leaked_key in (*sensitive_vars.keys(), "EXTRA_API_KEY", "EXTRA_SECRET", "EXTRA_PASSWORD", "EXTRA_TOKEN"):
        assert leaked_key not in sanitized

    # 2. Resource preexec helper builds cleanly on POSIX
    preexec = build_resource_preexec(memory_mb=512, cpu_seconds=30, max_procs=64)
    if os.name == "posix":
        assert callable(preexec)

    # 3. Real subprocess execution via AgentRuntimeExecutor.run_tool strips all sensitive host env vars
    executor = AgentRuntimeExecutor()
    env_dump_cmd = ["python3", "-c", "import json, os; print(json.dumps(dict(os.environ)))"]
    exec_res = await executor.run_tool(
        db_session,
        "worker-3",
        env_dump_cmd,
        policy=SandboxExecutionPolicy(sandbox_mode="process", memory_mb=512, cpu_seconds=30, max_procs=64),
    )
    assert exec_res["exit_code"] == 0, exec_res["stderr"]
    assert exec_res["sandbox_mode"] == "process"
    child_env_exec = json.loads(exec_res["stdout"])
    for k, v in sensitive_vars.items():
        assert k not in child_env_exec
        assert v not in exec_res["stdout"]
    assert child_env_exec.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert Path(child_env_exec["HOME"]).resolve() == (tmp_path / "workspaces" / "worker-3").resolve()

    # Disallowed binaries remain blocked fail-closed
    with pytest.raises(WorkspaceSandboxError):
        await executor.run_tool(db_session, "worker-3", ["bash", "-c", "env"])

    # 4. Real subprocess execution via WorkerDaemon._run_tool strips all sensitive host env vars
    daemon = WorkerDaemon(
        WorkerConfig(
            device_id="node-sandbox-test",
            token="daemon-tok-" + "never-expose-999",
            workspace_root=tmp_path / "daemon-ws",
        )
    )
    daemon_res = await daemon._run_tool(env_dump_cmd)
    assert daemon_res["exit_code"] == 0, daemon_res["stderr"]
    assert daemon_res["sandbox_mode"] == "process"
    child_env_daemon = json.loads(daemon_res["stdout"])
    for k, v in sensitive_vars.items():
        assert k not in child_env_daemon
        assert v not in daemon_res["stdout"]
    assert "never-expose-999" not in daemon_res["stdout"]
    assert Path(child_env_daemon["HOME"]).resolve() == (tmp_path / "daemon-ws").resolve()

    with pytest.raises(WorkspaceSandboxError):
        await daemon._run_tool(["curl", "https://example.com"])


@pytest.mark.asyncio
async def test_sandbox_docker_hardening_flags_and_fail_closed_when_cli_missing(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from app.runtime.executor import AgentRuntimeExecutor
    from app.runtime.sandbox import (
        WorkspaceSandboxError,
        build_isolated_command,
    )
    from app.runtime.worker_daemon import WorkerConfig, WorkerDaemon

    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")

    ws = (tmp_path / "ws-docker").resolve()
    ws.mkdir(parents=True, exist_ok=True)

    # 1. Hardened docker flags + host venv path rewritten to bare tool inside python:3.11-slim
    docker_cmd = build_isolated_command(
        ["/opt/venv/bin/pytest", "-q", "test_app.py"],
        ws,
        sandbox_mode="docker",
        memory_mb=256,
        cpu_limit=0.5,
        pids_limit=32,
        docker_bin="/usr/bin/docker",
    )
    assert docker_cmd[:6] == ["/usr/bin/docker", "run", "--rm", "--pull=never", "--network=none", "--read-only"]
    assert "--pull=never" in docker_cmd
    assert "--cap-drop=ALL" in docker_cmd
    assert "--security-opt=no-new-privileges" in docker_cmd
    assert "--pids-limit=32" in docker_cmd
    assert "--memory=256m" in docker_cmd
    assert "--cpus=0.5" in docker_cmd
    v_idx = docker_cmd.index("-v")
    assert docker_cmd[v_idx + 1] == f"{ws}:/workspace:rw"
    w_idx = docker_cmd.index("-w")
    assert docker_cmd[w_idx + 1] == "/workspace"
    assert "aatm-sandbox:latest" in docker_cmd
    assert docker_cmd[-3:] == ["pytest", "-q", "test_app.py"]

    # Disallowed binary and unsupported sandbox mode both fail closed
    with pytest.raises(WorkspaceSandboxError):
        build_isolated_command(["sh", "-c", "id"], ws, sandbox_mode="docker", docker_bin="/usr/bin/docker")
    with pytest.raises(WorkspaceSandboxError):
        build_isolated_command(["python3", "-V"], ws, sandbox_mode="privileged")

    # 2. Fail closed with WorkspaceSandboxError when `docker` CLI is absent on PATH
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(WorkspaceSandboxError, match="docker"):
        build_isolated_command(["python3", "-V"], ws, sandbox_mode="docker")

    executor = AgentRuntimeExecutor()
    with pytest.raises(WorkspaceSandboxError, match="docker"):
        await executor.run_tool(db_session, "worker-3", ["python3", "-V"], sandbox_mode="docker")

    daemon = WorkerDaemon(
        WorkerConfig(
            device_id="node-docker-missing",
            token="tok",
            workspace_root=tmp_path / "daemon-docker-ws",
            sandbox_mode="docker",
        )
    )
    with pytest.raises(WorkspaceSandboxError, match="docker"):
        await daemon._run_tool(["python3", "-V"])


@pytest.mark.asyncio
async def test_scheduler_automatic_degraded_session_recovery_and_cooldown(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.orchestration.scheduler import TaskScheduler
    from app.providers.router import provider_router

    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setenv("SCHEDULER_BRIDGE_TOKEN", "scheduler-recovery-token")

    with _MockBridgeServer(expected_bearer="scheduler-recovery-token") as srv:
        p_bridge = await db_session.get(AIProvider, "arena-local-bridge")
        assert p_bridge is not None
        p_bridge.endpoint_url = srv.base_url
        p_bridge.credential_ref = "env:SCHEDULER_BRIDGE_TOKEN"
        p_bridge.is_enabled = True

        acct = ProviderAccount(
            id="acct-sched-rec",
            provider_id="arena-local-bridge",
            label="Scheduler Recovery Account",
            account_identifier="sched-rec@local",
            status="active",
        )
        sess_deg = ProviderSession(
            id="sess-sched-deg-1",
            account_id=acct.id,
            agent_id="worker-3",
            status="degraded",
            health_score=20.0,
            error_count=4,
            last_error="Upstream 502",
        )
        proj = Project(
            id="proj-sched-r4",
            name="Scheduler Recovery Project",
            slug="sched-r4-proj",
            description="Test scheduler recovery + DAG/device pinning",
        )
        local_task = Task(
            id="task-sched-local",
            project_id=proj.id,
            division_code="D05",
            title="Local sched task",
            priority="high",
            status="queued",
            device_id="device-local",
        )
        remote_task = Task(
            id="task-sched-remote",
            project_id=proj.id,
            division_code="D05",
            title="Remote pinned task",
            priority="critical",
            status="queued",
            device_id="node-remote-99",
        )
        db_session.add_all([acct, sess_deg, proj, local_task, remote_task])
        await db_session.commit()

        srv.sessions[sess_deg.id] = acct.id

        scheduler = TaskScheduler(session_recovery_interval_seconds=60.0)
        cycle1 = await scheduler.dispatch_ready_tasks(db_session)

        # 1. Degraded session recovered automatically & bridge session cleared
        assert cycle1["session_recovery"]["skipped"] is False
        assert cycle1["session_recovery"]["recovered_count"] == 1
        assert sess_deg.id not in srv.sessions

        refreshed_sess = await db_session.get(ProviderSession, sess_deg.id)
        assert refreshed_sess is not None
        assert refreshed_sess.health_score == 100.0
        assert refreshed_sess.status == "active"
        assert refreshed_sess.error_count == 0

        # 2. Local task dispatched, remote device-pinned task NOT stolen
        dispatched_ids = [d["task_id"] for d in cycle1["dispatched"]]
        assert local_task.id in dispatched_ids
        assert remote_task.id not in dispatched_ids

        # 3. Second cycle within cooldown skips redundant recovery query
        cycle2 = await scheduler.run_orchestration_cycle(db_session)
        assert cycle2["session_recovery"]["skipped"] is True

        # 4. Forced recovery bypasses cooldown immediately
        refreshed_sess.health_score = 15.0
        refreshed_sess.status = "degraded"
        await db_session.commit()
        forced = await scheduler.recover_sessions_if_needed(db_session, force=True)
        assert forced["skipped"] is False
        assert forced["recovered_count"] == 1

        # 5. Best-effort resilience: recovery error never aborts task dispatching
        async def _boom_recover(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("transient recovery error")

        monkeypatch.setattr(provider_router, "recover_degraded_sessions", _boom_recover)
        local_task_2 = Task(
            id="task-sched-local-2",
            project_id=proj.id,
            division_code="D02",
            title="Task dispatched despite recovery error",
            priority="high",
            status="queued",
            device_id="device-local",
        )
        db_session.add(local_task_2)
        await db_session.commit()

        cycle3 = await scheduler.run_orchestration_cycle(db_session, force_session_recovery=True)
        assert "error" in cycle3["session_recovery"]
        assert any(d["task_id"] == local_task_2.id for d in cycle3["dispatched"])
