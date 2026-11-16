"""Item A — provider credential exfiltration guard.

credential_ref may only name provider API-key variables, server secrets are always
denied, endpoint_url is validated, operators are limited to official presets, and
every test-connection is audited without the key value.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import AuditLog, User
from app.providers.adapters import OpenAICompatibleAdapter, build_adapter
from app.providers.base import ProviderRequest
from app.providers.credential_policy import (
    SERVER_SECRET_ENV_DENYLIST,
    CredentialPolicyError,
    enforce_provider_config_policy,
    runtime_credential_denial,
    validate_endpoint_url,
    validate_provider_credential_ref,
)
from app.runtime.sandbox import SENSITIVE_ENV_EXACT

# URLs with embedded userinfo are assembled at runtime so the repo secret scanner stays clean.
_AT = "@"
USERINFO_URL_PASS = "https://user:pass" + _AT + "example.com/v1"
USERINFO_URL_TOKEN = "https://token" + _AT + "example.com/v1"
USERINFO_URL_LOCAL = "https://user:pw" + _AT + "127.0.0.1/v1"

SECRET_VALUE = "planted-server-secret-value-0123456789"
PROVIDER_KEY_VALUE = "planted-provider-key-value-abcdef"


@pytest.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        for email, role in (("a-operator@aatm.local", "operator"), ("a-admin@aatm.local", "admin")):
            db.add(
                User(
                    email=email,
                    full_name=email,
                    password_hash=hash_password("ItemAUniquePassword!2026"),
                    role=role,
                    must_change_password=False,
                    token_version=1,
                )
            )
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _headers(client: AsyncClient, email: str) -> dict[str, str]:
    from app.core.rate_limit import login_rate_limiter

    login_rate_limiter.reset()
    res = await client.post("/api/v1/auth/login", json={"email": email, "password": "ItemAUniquePassword!2026"})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


class _CaptureHandler(BaseHTTPRequestHandler):
    seen_auth: list[str] = []

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.__class__.seen_auth.append(self.headers.get("Authorization", ""))
        body = json.dumps(
            {
                "id": "x",
                "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_capture_server() -> tuple[HTTPServer, str]:
    _CaptureHandler.seen_auth = []
    srv = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


# --- unit level ---------------------------------------------------------------


def test_server_secret_denylist_reuses_sandbox_sensitive_env_exact():
    for name in SENSITIVE_ENV_EXACT - {"ANTHROPIC_API_KEY", "ARENA_BRIDGE_TOKEN"}:
        assert name in SERVER_SECRET_ENV_DENYLIST
    for name in ("SECRET_KEY", "DATABASE_URL", "DEFAULT_ADMIN_PASSWORD", "GITHUB_TOKEN", "GH_TOKEN"):
        with pytest.raises(CredentialPolicyError):
            validate_provider_credential_ref(f"env:{name}", provider_type="openai_compatible", endpoint_url=None)


def test_credential_ref_only_accepts_provider_api_key_names(monkeypatch: pytest.MonkeyPatch):
    ok = validate_provider_credential_ref(
        "env:OPENROUTER_API_KEY", provider_type="openai_compatible", endpoint_url="https://openrouter.ai/api/v1"
    )
    assert ok == "env:OPENROUTER_API_KEY"
    for bad in ("env:HOME", "env:PATH", "env:MY_TOKEN", "env:api_key", "env:_API_KEY", "sk-inline", "env:"):
        with pytest.raises((CredentialPolicyError, ValueError)):
            validate_provider_credential_ref(bad, provider_type="openai_compatible", endpoint_url=None)
    # ARENA_BRIDGE_UPSTREAM_API_KEY matches the pattern but is a server secret: denied.
    with pytest.raises(CredentialPolicyError):
        validate_provider_credential_ref(
            "env:ARENA_BRIDGE_UPSTREAM_API_KEY", provider_type="openai_compatible", endpoint_url=None
        )


def test_explicit_allowlist_extends_but_never_overrides_server_secret_denial(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROVIDER_CREDENTIAL_ENV_ALLOWLIST", "CUSTOM_LLM_TOKEN, SECRET_KEY, AATM_DEVICE_TOKEN")
    get_settings.cache_clear()
    try:
        assert validate_provider_credential_ref(
            "env:CUSTOM_LLM_TOKEN", provider_type="openai_compatible", endpoint_url=None
        )
        for denied in ("env:SECRET_KEY", "env:AATM_DEVICE_TOKEN"):
            with pytest.raises(CredentialPolicyError):
                validate_provider_credential_ref(denied, provider_type="openai_compatible", endpoint_url=None)
    finally:
        monkeypatch.delenv("PROVIDER_CREDENTIAL_ENV_ALLOWLIST", raising=False)
        get_settings.cache_clear()


def test_bridge_token_is_bound_to_local_bridge_only():
    assert validate_provider_credential_ref(
        "env:ARENA_BRIDGE_TOKEN", provider_type="arena_local_bridge", endpoint_url="http://127.0.0.1:4100"
    )
    with pytest.raises(CredentialPolicyError):
        validate_provider_credential_ref(
            "env:ARENA_BRIDGE_TOKEN", provider_type="arena_local_bridge", endpoint_url="https://attacker.example/v1"
        )
    with pytest.raises(CredentialPolicyError):
        validate_provider_credential_ref(
            "env:ARENA_BRIDGE_TOKEN", provider_type="openai_compatible", endpoint_url="http://127.0.0.1:4100"
        )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/v1",
        "file:///etc/passwd",
        "javascript:alert(1)",
        USERINFO_URL_PASS,
        USERINFO_URL_TOKEN,
        "https:///nohost",
        "https://example.com/v1#frag",
        "http://example.com:99999/v1",
    ],
)
def test_endpoint_url_validation_rejects_unsafe_urls(url: str):
    with pytest.raises(CredentialPolicyError):
        validate_endpoint_url(url)


def test_operator_limited_to_official_presets_admin_may_use_custom():
    enforce_provider_config_policy(
        role="operator",
        provider_type="openai_compatible",
        endpoint_url="https://openrouter.ai/api/v1",
        credential_ref="env:OPENROUTER_API_KEY",
    )
    with pytest.raises(CredentialPolicyError) as exc:
        enforce_provider_config_policy(
            role="operator",
            provider_type="openai_compatible",
            endpoint_url="https://evil.example/v1",
            credential_ref="env:OPENROUTER_API_KEY",
        )
    assert exc.value.status_code == 403
    # Preset host, but a different key than the preset pairs with -> admin only (cross-provider key leak).
    with pytest.raises(CredentialPolicyError):
        enforce_provider_config_policy(
            role="operator",
            provider_type="openai_compatible",
            endpoint_url="https://openrouter.ai/api/v1",
            credential_ref="env:ANTHROPIC_API_KEY",
        )
    enforce_provider_config_policy(
        role="admin",
        provider_type="openai_compatible",
        endpoint_url="https://custom-llm.example/v1",
        credential_ref="env:CUSTOM_API_KEY",
    )


async def test_runtime_adapter_never_resolves_server_secrets_for_legacy_rows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SECRET_KEY", SECRET_VALUE)
    srv, base_url = _start_capture_server()
    try:
        adapter = OpenAICompatibleAdapter("legacy-row", base_url, "env:SECRET_KEY", "m")
        assert adapter.resolve_credential() is None
        resp = await adapter.generate(ProviderRequest(prompt="hi", model="m", dry_run=False))
        assert resp.success is False
        assert all(SECRET_VALUE not in h for h in _CaptureHandler.seen_auth)
        assert runtime_credential_denial("env:SECRET_KEY", provider_type="openai_compatible", endpoint_url=base_url)
        bridge = build_adapter("arena_local_bridge", "b", "https://attacker.example", "env:ARENA_BRIDGE_TOKEN", "m")
        assert bridge.credential_ref_valid is False
    finally:
        srv.shutdown()


# --- API level ----------------------------------------------------------------


async def test_wizard_test_connection_refuses_secret_exfiltration_and_audits(client, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET_VALUE)
    monkeypatch.setenv("DATABASE_URL_COPY_API_KEY", PROVIDER_KEY_VALUE)
    admin = await _headers(client, "a-admin@aatm.local")
    srv, base_url = _start_capture_server()
    try:
        for ref in ("env:SECRET_KEY", "env:DATABASE_URL", "env:DEFAULT_ADMIN_PASSWORD", "env:GITHUB_TOKEN", "env:HOME"):
            res = await client.post(
                "/api/v1/providers/wizard/test-connection",
                headers=admin,
                json={"provider_type": "openai_compatible", "endpoint_url": base_url, "credential_ref": ref, "model": "m"},
            )
            assert res.status_code == 400, (ref, res.text)
            assert res.json()["code"] == "CREDENTIAL_POLICY_VIOLATION"
        assert _CaptureHandler.seen_auth == []

        res = await client.post(
            "/api/v1/providers/wizard/test-connection",
            headers=admin,
            json={
                "provider_type": "openai_compatible",
                "endpoint_url": USERINFO_URL_LOCAL,
                "credential_ref": "env:DATABASE_URL_COPY_API_KEY",
                "model": "m",
            },
        )
        assert res.status_code == 400

        ok = await client.post(
            "/api/v1/providers/wizard/test-connection",
            headers=admin,
            json={
                "provider_type": "openai_compatible",
                "endpoint_url": base_url,
                "credential_ref": "env:DATABASE_URL_COPY_API_KEY",
                "model": "m",
            },
        )
        assert ok.status_code == 200 and ok.json()["success"] is True
    finally:
        srv.shutdown()

    async with AsyncSessionLocal() as db:
        logs = list(
            (
                await db.execute(
                    select(AuditLog).where(AuditLog.action.in_(["provider.test_connection", "provider.test_connection_denied"]))
                )
            ).scalars()
        )
    assert len([log for log in logs if log.action == "provider.test_connection_denied"]) == 6
    success = [log for log in logs if log.action == "provider.test_connection"]
    assert len(success) == 1 and success[0].details["credential_ref"] == "env:DATABASE_URL_COPY_API_KEY"
    dumped = json.dumps([log.details for log in logs])
    assert SECRET_VALUE not in dumped and PROVIDER_KEY_VALUE not in dumped


async def test_operator_cannot_test_or_configure_custom_endpoint(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", PROVIDER_KEY_VALUE)
    operator = await _headers(client, "a-operator@aatm.local")
    srv, base_url = _start_capture_server()
    try:
        for path, extra in (
            ("/api/v1/providers/wizard/test-connection", {"model": "m"}),
            ("/api/v1/providers/wizard/configure", {"default_model": "m"}),
        ):
            res = await client.post(
                path,
                headers=operator,
                json={
                    "provider_type": "openai_compatible",
                    "endpoint_url": base_url.replace("127.0.0.1", "localhost"),
                    "credential_ref": "env:OPENROUTER_API_KEY",
                    **extra,
                },
            )
            assert res.status_code == 403, res.text
            assert res.json()["code"] == "CUSTOM_ENDPOINT_REQUIRES_ADMIN"
        res = await client.post(
            "/api/v1/providers",
            headers=operator,
            json={
                "name": "Evil",
                "provider_type": "openai_compatible",
                "endpoint_url": "https://evil.example/v1",
                "credential_ref": "env:OPENROUTER_API_KEY",
                "default_model": "m",
            },
        )
        assert res.status_code == 403
        assert _CaptureHandler.seen_auth == []
    finally:
        srv.shutdown()

    preset = await client.post(
        "/api/v1/providers",
        headers=operator,
        json={
            "name": "OpenRouter",
            "provider_type": "openai_compatible",
            "endpoint_url": "https://openrouter.ai/api/v1",
            "credential_ref": "env:OPENROUTER_API_KEY",
            "default_model": "m",
        },
    )
    assert preset.status_code == 201


async def test_provider_upsert_and_account_create_reject_server_secrets(client):
    admin = await _headers(client, "a-admin@aatm.local")
    res = await client.post(
        "/api/v1/providers",
        headers=admin,
        json={
            "name": "Leaky",
            "provider_type": "openai_compatible",
            "endpoint_url": "https://custom.example/v1",
            "credential_ref": "env:SECRET_KEY",
            "default_model": "m",
        },
    )
    assert res.status_code == 400
    acc = await client.post(
        "/api/v1/accounts",
        headers=admin,
        json={"provider_id": "anthropic-claude", "label": "x", "account_identifier": "x", "credential_ref": "env:SECRET_KEY"},
    )
    assert acc.status_code == 400
    arena_acc = await client.post(
        "/api/v1/accounts",
        headers=admin,
        json={"provider_id": "arena-agent", "label": "Arena seat", "account_identifier": "seat-1"},
    )
    assert arena_acc.status_code == 201
    assert arena_acc.json()["credential_ref"] is None
