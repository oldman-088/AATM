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
from sqlalchemy import select

from app.browser.engines import (
    BROWSER_ENGINE_REGISTRY,
    authorize_userscript_execution,
    match_url_pattern,
    validate_userscript_policy,
)
from app.browser.manager import (
    BrowserManager,
    detect_playwright_capabilities,
    validate_navigation_url,
    validate_selector,
)
from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
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


# ---------------------------------------------------------------------------
# Division 05: AI Providers, Credential Indirection, Budgets & Failover
# ---------------------------------------------------------------------------


async def test_credential_ref_indirection_blocks_raw_secrets():
    assert validate_credential_ref(None)[0] is True
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

        # Dry-run never spends budget
        dry = await adapter.generate(ProviderRequest(prompt="test", model="arena-reasoning-pro", dry_run=True))
        assert dry.success is True
        assert dry.estimated_cost_usd == 0.0
        assert dry.metadata["dry_run"] is True
        assert dry.metadata["projected_cost_usd"] > 0.0

        # Live call reads OpenAI-compatible usage & content
        live = await adapter.generate(ProviderRequest(prompt="ping", dry_run=False))
        assert live.success is True
        assert live.content == "Bridge live pong"
        assert live.input_tokens == 11
        assert live.output_tokens == 7
        assert live.metadata["confirmed_usage"] is True

        # Empty choices array handled gracefully without IndexError
        srv.empty_choices = True
        empty_resp = await adapter.generate(ProviderRequest(prompt="ping", dry_run=False))
        assert empty_resp.success is True
        assert empty_resp.content == ""

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
    assert diag.success is True
    assert diag.estimated_cost_usd == 0.0
    assert diag.metadata["dry_run"] is True

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
        assert logs[0].confirmed_cost_usd == resp.estimated_cost_usd


async def test_router_daily_monthly_spend_caps_and_failover(db_session):
    router = ProviderRouter()

    # Point arena-local-bridge to an unused loopback port so it is guaranteed offline
    bridge = await db_session.get(AIProvider, "arena-local-bridge")
    bridge.endpoint_url = "http://127.0.0.1:1"
    await db_session.commit()

    # Requesting a bridge-specific model fails over cleanly to arena-agent using arena-agent's compatible default model
    failover = await router.route_and_execute(
        db_session,
        ProviderRequest(prompt="Failover test", model="arena-reasoning-pro", dry_run=False),
        preferred_provider_id="arena-local-bridge",
    )
    assert failover.success is True
    assert failover.provider_id == "arena-agent"
    assert failover.model == "arena-agent-orchestrator"

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
    assert dry_ok.estimated_cost_usd == 0.0
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
    assert caps["engine_mode"] in ("playwright", "headless_fallback")
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


async def test_browser_db_file_async_locks_stale_recovery_and_crash(db_session, tmp_path):
    mgr = BrowserManager()
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
    assert pw_caps["status"] == ("ready" if real_caps["chromium_installed"] else "headless_fallback")

    # Prohibited action rejected across engines
    rej = await BROWSER_ENGINE_REGISTRY["playwright"].execute_action("profile-1", "bypass_captcha", {})
    assert rej["status"] == "rejected"

    # Automa workflow validates safe vs. unsafe steps
    automa = BROWSER_ENGINE_REGISTRY["automa"]
    good_wf = await automa.execute_action(
        "profile-1",
        "run_workflow",
        {"steps": [{"type": "navigate", "url": "https://fastapi.tiangolo.com"}, {"type": "snapshot"}]},
    )
    assert good_wf["status"] == "executed"
    assert good_wf["steps_validated"] == 2

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

    fill_res = await BROWSER_ENGINE_REGISTRY["playwright"].execute_action(
        "profile-1", "fill_input", {"selector": "input[name='query']", "value": "private-query-value-42"}
    )
    assert fill_res["status"] in ("executed", "headless_fallback")
    assert fill_res["params"]["value"] == "<redacted>"
    assert fill_res["params"]["value_length"] == len("private-query-value-42")

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


async def test_browser_safe_actions_click_fill_snapshot_fallback_and_playwright_live(db_session, tmp_path, monkeypatch):
    # Selector policy validation unit checks
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

    # Clicking or filling a stopped profile is rejected cleanly
    with pytest.raises(ValueError, match="must be running"):
        await mgr.click_element(db_session, bp, "#save-btn", actor="worker-3")
    with pytest.raises(ValueError, match="must be running"):
        await mgr.fill_input(db_session, bp, "input[name='note']", "hello", actor="worker-3")

    # Force headless_fallback (Chromium absent) and start profile
    monkeypatch.setattr(
        "app.browser.manager.detect_playwright_capabilities",
        lambda: {
            "playwright_installed": True,
            "chromium_installed": False,
            "chromium_path": None,
            "engine_mode": "headless_fallback",
        },
    )
    await mgr.launch_profile(db_session, bp, owner="worker-3", mode="background")
    await mgr.navigate_profile(db_session, bp, "https://fastapi.tiangolo.com/tutorial/")

    # Prohibited CAPTCHA selector blocked even on a running profile
    with pytest.raises(ValueError, match="Policy violation"):
        await mgr.click_element(db_session, bp, "#recaptcha-anchor", actor="worker-3")
    with pytest.raises(ValueError, match="Policy violation"):
        await mgr.fill_input(db_session, bp, ".h-captcha textarea", "nope", actor="worker-3")

    # 1. Headless fallback path: click_element, fill_input (redacted!), capture_snapshot
    click_fb = await mgr.click_element(db_session, bp, "#nav-docs", actor="worker-3")
    assert click_fb["ok"] is True
    assert click_fb["engine_mode"] == "headless_fallback"
    assert click_fb["executed_live"] is False
    assert "fallback_reason" in click_fb

    sensitive_val = "confidential-form-entry-987654"
    fill_fb = await mgr.fill_input(db_session, bp, "input[name='notes']", sensitive_val, actor="worker-3")
    assert fill_fb["ok"] is True
    assert fill_fb["engine_mode"] == "headless_fallback"
    assert fill_fb["executed_live"] is False
    assert fill_fb["redacted"] is True
    assert fill_fb["value_length"] == len(sensitive_val)
    # Raw input text MUST NOT appear in SVG or AuditLog
    assert sensitive_val not in (bp.last_snapshot_svg or "")
    logs = (await db_session.execute(select(AuditLog).where(AuditLog.resource_id == bp.id))).scalars().all()
    assert all(sensitive_val not in json.dumps(log.details) for log in logs)

    snap_fb = await mgr.capture_snapshot(db_session, bp, actor="worker-3")
    assert snap_fb["ok"] is True
    assert snap_fb["engine_mode"] == "headless_fallback"
    assert snap_fb["executed_live"] is False
    assert snap_fb["format"] == "svg"
    assert snap_fb["screenshot_base64"] is None
    assert "<svg" in snap_fb["svg"]
    assert snap_fb["lock_status"]["file_lock_present"] is True

    # 2. Simulated live Playwright context path: verifies live click, fill, and PNG+SVG snapshot
    class _FakePage:
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

    class _FakeContext:
        def __init__(self, page: _FakePage) -> None:
            self.pages = [page]

        async def close(self) -> None:
            return None

    class _FakePlaywright:
        async def stop(self) -> None:
            return None

    fake_page = _FakePage()
    mgr._active_contexts[bp.id] = (_FakePlaywright(), _FakeContext(fake_page))

    live_click = await mgr.click_element(db_session, bp, "button.primary-submit", actor="worker-3")
    assert live_click["ok"] is True
    assert live_click["engine_mode"] == "playwright"
    assert live_click["executed_live"] is True
    assert fake_page.clicked == ["button.primary-submit"]
    assert live_click["page_title"] == "FastAPI Tutorial Live"

    live_val = "live-redacted-field-xyz"
    live_fill = await mgr.fill_input(db_session, bp, "textarea#summary", live_val, actor="worker-3")
    assert live_fill["ok"] is True
    assert live_fill["engine_mode"] == "playwright"
    assert live_fill["executed_live"] is True
    assert live_fill["redacted"] is True
    assert fake_page.filled == [("textarea#summary", live_val)]
    assert live_val not in (bp.last_snapshot_svg or "")

    live_snap = await mgr.capture_snapshot(db_session, bp, actor="worker-3", full_page=True)
    assert live_snap["ok"] is True
    assert live_snap["engine_mode"] == "playwright"
    assert live_snap["executed_live"] is True
    assert live_snap["format"] == "png+svg"
    assert live_snap["screenshot_bytes"] == len(b"\x89PNG\r\n\x1a\nFAKE_PNG_PAYLOAD")
    assert base64.b64decode(live_snap["screenshot_base64"]) == b"\x89PNG\r\n\x1a\nFAKE_PNG_PAYLOAD"
    assert "<svg" in live_snap["svg"]

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
