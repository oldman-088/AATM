"""Phase 1 & 1B — Security Hardening verification suite."""

import asyncio
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main_module
from alembic import command
from app.core.config import ROOT_DIR, Settings, get_settings, validate_runtime_security
from app.core.events import event_bus
from app.core.rate_limit import login_rate_limiter
from app.core.security import create_access_token, decode_access_token, hash_password
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import AuditLog, Project, User
from app.runtime.executor import AgentRuntimeExecutor
from app.runtime.sandbox import SandboxExecutionPolicy, WorkspaceSandboxError, build_isolated_command

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG_PATH = BACKEND_ROOT / "alembic.ini"


@pytest.fixture(scope="module")
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)

    login_rate_limiter.reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="module")
async def admin_headers(client: AsyncClient):
    """Non-default admin account so seeded admin@aatm.local keeps must_change_password=True."""
    email = "phase1-admin@aatm.local"
    admin_secret = "Phase1Admin" + "StrongSecret!2026"
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is None:
            db.add(
                User(
                    email=email,
                    full_name="Phase 1 Admin",
                    password_hash=hash_password(admin_secret),
                    role="admin",
                    must_change_password=False,
                    token_version=1,
                )
            )
            await db.commit()

    login_rate_limiter.reset()
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": admin_secret})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _concrete_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "nonexistent-id", path)


async def test_all_non_public_routes_return_401_without_auth(client: AsyncClient):
    """Step 0: enumerate main's full OpenAPI & router surface across all HTTP methods."""
    public_allowlist = {
        ("GET", "/api/health"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/redoc"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/github/webhooks"),
    }
    checked: list[tuple[str, str]] = []

    def _iter_api_routes(routes, prefix: str = ""):
        for route in routes:
            if isinstance(route, APIRoute):
                yield f"{prefix}{route.path}", route.methods or set()
            elif hasattr(route, "effective_route_contexts"):
                for ctx in route.effective_route_contexts():
                    yield f"{prefix}{ctx.path}", ctx.methods or set()

    openapi_ops = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if path.startswith("/api")
    }
    discovered_ops = {
        (method.upper(), path)
        for path, methods in _iter_api_routes(app.routes)
        for method in methods
        if path.startswith("/api") and method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    assert openapi_ops.issubset(discovered_ops)

    for method, full_path in sorted(discovered_ops):
        if (method, full_path) in public_allowlist:
            continue
        kwargs = {"json": {}} if method in {"POST", "PUT", "PATCH"} else {}
        resp = await client.request(method, _concrete_path(full_path), **kwargs)
        assert resp.status_code == 401, f"{method} {full_path} returned {resp.status_code} instead of 401"
        checked.append((method, full_path))

    # Public routes explicitly behave as designed without JWT
    assert (await client.get("/api/health")).status_code == 200
    assert (await client.post("/api/v1/github/webhooks", json={})).status_code in {401, 503}

    assert ("GET", "/api/v1/dashboard/overview") in checked
    assert ("GET", "/api/v1/projects") in checked
    assert ("GET", "/api/v1/git/status") in checked
    assert ("GET", "/api/v1/devices") in checked
    assert ("POST", "/api/v1/devices/{device_id}/tasks/{task_id}/model") in checked
    assert ("POST", "/api/v1/employees/provision-specialists") in checked
    assert len(checked) >= 60


async def test_write_routes_require_admin_or_operator_role(client: AsyncClient):
    viewer_email = "viewer-phase1@example.test"
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == viewer_email))).scalar_one_or_none()
        if existing is None:
            db.add(
                User(
                    email=viewer_email,
                    full_name="Readonly Viewer",
                    password_hash=hash_password("viewer-strong-password-2026"),
                    role="viewer",
                    must_change_password=False,
                    token_version=1,
                )
            )
            await db.commit()

    viewer_headers = {"Authorization": f"Bearer {create_access_token(subject=viewer_email, role='viewer', token_version=1)}"}

    # Viewer can read protected GET endpoints
    read_resp = await client.get("/api/v1/dashboard/overview", headers=viewer_headers)
    assert read_resp.status_code == 200

    # Viewer is rejected (403) on operator/admin write routes (including PR #5 routes)
    for method, path, payload in (
        ("POST", "/api/v1/projects", {"name": "Blocked Viewer Project"}),
        ("POST", "/api/v1/providers", {"name": "Blocked Provider", "provider_type": "openai_compatible", "default_model": "gpt-4o"}),
        ("POST", "/api/v1/approvals", {"action_type": "merge", "title": "Blocked Approval"}),
        ("POST", "/api/v1/orchestration/cycle", None),
        ("POST", "/api/v1/emergency-stop", {"scope": "global", "reason": "test"}),
        ("POST", "/api/v1/employees/provision-specialists", {"required_skills": ["python"]}),
        ("POST", "/api/v1/providers/sessions/recover", None),
        ("POST", "/api/v1/git/worktrees", {"agent_id": "worker-1", "branch_name": "feature/x"}),
        ("POST", "/api/v1/devices/node-1/revoke", None),
        ("POST", "/api/v1/browsers/profile-1/workflow", {"steps": []}),
        ("POST", "/api/v1/github/prs/1/comments", {"repo": "oldman-088/AATM", "body": "blocked"}),
    ):
        resp = await client.request(method, path, headers=viewer_headers, json=payload)
        assert resp.status_code == 403, f"{method} {path} allowed viewer with {resp.status_code}"


async def test_git_status_rejects_unregistered_repo_path(client: AsyncClient, admin_headers, tmp_path: Path):
    ok_default = await client.get("/api/v1/git/status", headers=admin_headers)
    assert ok_default.status_code == 200

    for bad_path in ("/etc", "/tmp", str(tmp_path), "../../etc"):
        denied = await client.get("/api/v1/git/status", params={"repo_path": bad_path}, headers=admin_headers)
        assert denied.status_code == 403, f"Expected 403 for {bad_path}, got {denied.status_code}"

    reg_dir = (tmp_path / "registered-repo").resolve()
    reg_dir.mkdir(parents=True, exist_ok=True)
    async with AsyncSessionLocal() as db:
        db.add(
            Project(
                name="Phase1 Registered Repo",
                slug="phase1-registered-repo",
                description="Registered repo test",
                repo_path=str(reg_dir),
            )
        )
        await db.commit()

    ok_registered = await client.get("/api/v1/git/status", params={"repo_path": str(reg_dir)}, headers=admin_headers)
    assert ok_registered.status_code == 200


async def test_lifespan_runs_security_and_starts_and_cancels_tasks(monkeypatch: pytest.MonkeyPatch):
    """Step 0: lifespan validates runtime security and manages BackgroundScheduler + AgentLoop."""
    from app.runtime.agent_loop import AgentLoop
    from app.runtime.background_scheduler import BackgroundScheduler

    events: list[str] = []
    cancelled: list[str] = []

    def spy_validate(s):
        events.append("validate_runtime_security")

    async def fake_orch(self):
        events.append("scheduler.orchestration.start")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append("scheduler.orchestration")
            raise

    async def fake_heal(self):
        events.append("scheduler.self_healing.start")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append("scheduler.self_healing")
            raise

    async def fake_agent_loop(self):
        events.append("agent_loop.start")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append("agent_loop")
            raise

    monkeypatch.setattr(main_module, "validate_runtime_security", spy_validate)
    monkeypatch.setattr(BackgroundScheduler, "run_orchestration_forever", fake_orch)
    monkeypatch.setattr(BackgroundScheduler, "run_self_healing_forever", fake_heal)
    monkeypatch.setattr(AgentLoop, "run_forever", fake_agent_loop)
    monkeypatch.setattr(get_settings(), "AGENT_LOOP_ENABLED", True)

    async with main_module.lifespan(app):
        await asyncio.sleep(0.02)
        assert "validate_runtime_security" in events
        assert "scheduler.orchestration.start" in events
        assert "scheduler.self_healing.start" in events
        assert "agent_loop.start" in events

    assert set(cancelled) == {"scheduler.orchestration", "scheduler.self_healing", "agent_loop"}


def _disable_background_lifespan_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.runtime.background_scheduler import BackgroundScheduler

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(BackgroundScheduler, "run_orchestration_forever", _noop)
    monkeypatch.setattr(BackgroundScheduler, "run_self_healing_forever", _noop)
    monkeypatch.setattr(get_settings(), "AGENT_LOOP_ENABLED", False)


def test_websocket_requires_first_message_jwt_and_rejects_query_param(monkeypatch: pytest.MonkeyPatch):
    """Defect 7: WebSocket accepts JWT only via first message; ?token= query strings are ignored."""
    _disable_background_lifespan_loops(monkeypatch)
    monkeypatch.setattr(main_module, "WS_AUTH_TIMEOUT_SECONDS", 0.15)
    token = create_access_token(subject="phase1-admin@aatm.local", role="admin", token_version=1)

    with TestClient(app) as sync_client:
        # 1. Missing token times out quickly and closes with 4401
        with sync_client.websocket_connect("/ws/events") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4401

        # 2. Query-string ?token= alone must NOT authenticate (times out with 4401)
        with sync_client.websocket_connect(f"/ws/events?token={token}") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4401

        # 3. Invalid token closes immediately with 4401
        with sync_client.websocket_connect("/ws/events") as ws:
            ws.send_text(json.dumps({"token": "invalid.jwt.token"}))
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4401

        # 4. Valid token via first message authenticates
        with sync_client.websocket_connect("/ws/events") as ws:
            ws.send_text(json.dumps({"token": token}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "auth.ok"


async def test_websocket_emits_only_to_authenticated_sockets(monkeypatch: pytest.MonkeyPatch):
    _disable_background_lifespan_loops(monkeypatch)
    monkeypatch.setattr(main_module, "WS_AUTH_TIMEOUT_SECONDS", 0.15)
    token = create_access_token(subject="phase1-admin@aatm.local", role="admin", token_version=1)

    with TestClient(app) as sync_client:
        with sync_client.websocket_connect("/ws/events") as ws:
            ws.send_text(json.dumps({"token": token}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "auth.ok"
            assert len(event_bus._connections) == 1
            evt = await event_bus.emit("phase1.auth_only", {"ok": True})
            received = json.loads(ws.receive_text())
            assert received["event_id"] == evt["event_id"]
            assert received["type"] == "phase1.auth_only"


def test_production_settings_validator_and_network_defaults():
    defaults = Settings()
    assert defaults.HOST == "127.0.0.1"
    assert defaults.get_cors_origins() == ["http://localhost:5173"]
    assert defaults.get_trusted_proxies() == set()
    assert defaults.AATM_SANDBOX_MODE in {"docker", os.environ.get("AATM_SANDBOX_MODE", "docker")}

    csv_cors = Settings(CORS_ORIGINS="https://app.aatm.ai, https://ops.aatm.ai")
    assert csv_cors.get_cors_origins() == ["https://app.aatm.ai", "https://ops.aatm.ai"]

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_runtime_security(
            Settings(
                ENVIRONMENT="production",
                DEFAULT_ADMIN_PASSWORD="Strong-Admin-Password-2026!",
                AATM_SANDBOX_MODE="docker",
            )
        )

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_runtime_security(
            Settings(
                ENVIRONMENT="production",
                SECRET_KEY="too-short",
                DEFAULT_ADMIN_PASSWORD="Strong-Admin-Password-2026!",
                AATM_SANDBOX_MODE="docker",
            )
        )

    with pytest.raises(RuntimeError, match="DEFAULT_ADMIN_PASSWORD"):
        validate_runtime_security(
            Settings(
                ENVIRONMENT="production",
                SECRET_KEY="a" * 64,
                DEFAULT_ADMIN_PASSWORD="aatm-admin-2026",
                AATM_SANDBOX_MODE="docker",
            )
        )

    with pytest.raises(RuntimeError, match="AATM_SANDBOX_MODE=process"):
        validate_runtime_security(
            Settings(
                ENVIRONMENT="production",
                SECRET_KEY="a" * 64,
                DEFAULT_ADMIN_PASSWORD="Strong-Admin-Password-2026!",
                AATM_SANDBOX_MODE="process",
                AATM_ALLOW_PROCESS_SANDBOX=False,
            )
        )

    validate_runtime_security(
        Settings(
            ENVIRONMENT="production",
            SECRET_KEY="a" * 64,
            DEFAULT_ADMIN_PASSWORD="Strong-Admin-Password-2026!",
            AATM_SANDBOX_MODE="docker",
        )
    )
    validate_runtime_security(
        Settings(
            ENVIRONMENT="production",
            SECRET_KEY="a" * 64,
            DEFAULT_ADMIN_PASSWORD="Strong-Admin-Password-2026!",
            AATM_SANDBOX_MODE="process",
            AATM_ALLOW_PROCESS_SANDBOX=True,
        )
    )


async def test_login_rate_limit_ignores_untrusted_xff_and_enforces_per_email_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Defect 1: rotating X-Forwarded-For cannot bypass login rate limits."""
    login_rate_limiter.reset()
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXIES", "")

    untrusted_email = "rate-limit-untrusted@aatm.local"
    for idx in range(5):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": untrusted_email, "password": f"wrong-password-{idx}"},
            headers={"X-Forwarded-For": f"203.0.113.{10 + idx}"},
        )
        assert resp.status_code == 401

    blocked = await client.post(
        "/api/v1/auth/login",
        json={"email": untrusted_email, "password": "wrong-password-final"},
        headers={"X-Forwarded-For": "203.0.113.250"},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1

    async with AsyncSessionLocal() as db:
        audits = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.actor == untrusted_email,
                    AuditLog.action.in_(["auth.login_failed", "auth.login_rate_limited"]),
                )
            )
        ).scalars().all()
    actions = [a.action for a in audits]
    assert actions.count("auth.login_failed") == 5
    assert "auth.login_rate_limited" in actions
    # Spoofed X-Forwarded-For must NOT be trusted when TRUSTED_PROXIES is empty
    assert all(not str(a.details.get("ip", "")).startswith("203.0.113.") for a in audits)

    # Even when TRUSTED_PROXIES trusts the peer and X-Forwarded-For rotates per attempt,
    # the independent per-email rate limit still blocks on the 6th attempt.
    login_rate_limiter.reset()
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXIES", "127.0.0.1,testclient")
    trusted_email = "rate-limit-trusted-xff@aatm.local"
    for idx in range(5):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": trusted_email, "password": f"wrong-{idx}"},
            headers={"X-Forwarded-For": f"198.51.100.{20 + idx}"},
        )
        assert resp.status_code == 401

    blocked_rotating = await client.post(
        "/api/v1/auth/login",
        json={"email": trusted_email, "password": "wrong-final"},
        headers={"X-Forwarded-For": "198.51.100.250"},
    )
    assert blocked_rotating.status_code == 429
    assert int(blocked_rotating.headers["Retry-After"]) >= 1

    async with AsyncSessionLocal() as db:
        trusted_audits = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.actor == trusted_email,
                    AuditLog.action == "auth.login_rate_limited",
                )
            )
        ).scalars().all()
    assert trusted_audits[0].details.get("ip") == "198.51.100.250"
    login_rate_limiter.reset()


async def test_must_change_password_enforced_on_api_and_websocket(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Defect 2: must_change_password=True blocks all endpoints except /auth/me & /auth/change-password and rejects WS."""
    _disable_background_lifespan_loops(monkeypatch)
    monkeypatch.setattr(main_module, "WS_AUTH_TIMEOUT_SECONDS", 0.15)
    login_rate_limiter.reset()
    settings = get_settings()

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert login_resp.status_code == 200
    assert login_resp.json()["user"]["must_change_password"] is True
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # /auth/me is allowed
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True

    # Every other endpoint returns 403 {"code": "PASSWORD_CHANGE_REQUIRED"}
    for method, path, payload in (
        ("GET", "/api/v1/dashboard/overview", None),
        ("GET", "/api/v1/projects", None),
        ("POST", "/api/v1/projects", {"name": "Blocked"}),
        ("PATCH", "/api/v1/auth/preferences", {"theme": "light"}),
    ):
        resp = await client.request(method, path, headers=headers, json=payload)
        assert resp.status_code == 403, f"{method} {path} returned {resp.status_code}"
        assert resp.json()["code"] == "PASSWORD_CHANGE_REQUIRED"

    # WebSocket rejects with 4403
    with TestClient(app) as sync_client:
        with sync_client.websocket_connect("/ws/events") as ws:
            ws.send_text(json.dumps({"token": token}))
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4403


async def test_token_version_and_change_password_validation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Defects 4 & 8: token_version invalidates old JWTs on password change; placeholders & email rejected."""
    _disable_background_lifespan_loops(monkeypatch)
    monkeypatch.setattr(main_module, "WS_AUTH_TIMEOUT_SECONDS", 0.15)
    login_rate_limiter.reset()

    user_email = "token-version-user@aatm.local"
    initial_pw = "InitialUniquePassword!2026"
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == user_email))).scalar_one_or_none()
        if existing is None:
            db.add(
                User(
                    email=user_email,
                    full_name="Token Version User",
                    password_hash=hash_password(initial_pw),
                    role="operator",
                    must_change_password=False,
                    token_version=1,
                )
            )
            await db.commit()

    login_resp = await client.post("/api/v1/auth/login", json={"email": user_email, "password": initial_pw})
    assert login_resp.status_code == 200
    old_token = login_resp.json()["access_token"]
    old_headers = {"Authorization": f"Bearer {old_token}"}
    assert decode_access_token(old_token)["token_version"] == 1

    # Wrong current password -> 401
    bad_cur = await client.post(
        "/api/v1/auth/change-password",
        headers=old_headers,
        json={"current_password": "wrong", "new_password": "NewStrongPassword!2026"},
    )
    assert bad_cur.status_code == 401

    # Defect 8: reject placeholder/default passwords AND password equal to user's email
    for forbidden_pw in ("aatm-admin-2026", "change-me-immediately", user_email, user_email.upper()):
        rejected = await client.post(
            "/api/v1/auth/change-password",
            headers=old_headers,
            json={"current_password": initial_pw, "new_password": forbidden_pw},
        )
        assert rejected.status_code == 400, f"Password {forbidden_pw!r} should have been rejected"

    # Rotate password -> token_version increments and old token is immediately rejected
    rotated_pw = "RotatedStrongPassword!2026"
    ok = await client.post(
        "/api/v1/auth/change-password",
        headers=old_headers,
        json={"current_password": initial_pw, "new_password": rotated_pw},
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["must_change_password"] is False
    new_token = ok.json()["access_token"]
    assert new_token != old_token
    assert decode_access_token(new_token)["token_version"] == 2

    # Old token is rejected on HTTP endpoints AND on WebSocket
    assert (await client.get("/api/v1/auth/me", headers=old_headers)).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_token}"})).status_code == 200

    with TestClient(app) as sync_client:
        with sync_client.websocket_connect("/ws/events") as ws:
            ws.send_text(json.dumps({"token": old_token}))
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4401

        with sync_client.websocket_connect("/ws/events") as ws:
            ws.send_text(json.dumps({"token": new_token}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "auth.ok"


async def test_existing_install_default_passwords_flagged_by_migration_and_startup(tmp_path: Path, monkeypatch):
    """Defect 3: existing users with default/placeholder passwords are flagged must_change_password=True."""
    db_path = tmp_path / "existing-install.sqlite"
    sync_url = f"sqlite:///{db_path.as_posix()}"
    async_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("SYNC_DATABASE_URL", sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    get_settings.cache_clear()
    try:
        config = Config(str(ALEMBIC_CONFIG_PATH))
        config.set_main_option("path_separator", "os")
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        command.upgrade(config, "9a4e3d2c1b0f")

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO users (
                    id, email, full_name, password_hash, role, language, theme,
                    is_active, must_change_password, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-default-admin",
                    "legacy-admin@aatm.local",
                    "Legacy Admin",
                    hash_password("aatm-admin-2026"),
                    "admin",
                    "en",
                    "dark",
                    1,
                    0,
                    "2026-01-01 00:00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO users (
                    id, email, full_name, password_hash, role, language, theme,
                    is_active, must_change_password, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-custom-user",
                    "legacy-custom@aatm.local",
                    "Custom User",
                    hash_password("UniqueCustomPassword!2026"),
                    "operator",
                    "en",
                    "dark",
                    1,
                    0,
                    "2026-01-01 00:00:00",
                ),
            )
            conn.commit()

        command.upgrade(config, "head")
        with sqlite3.connect(db_path) as conn:
            default_row = conn.execute(
                "SELECT must_change_password, token_version FROM users WHERE id = ?",
                ("legacy-default-admin",),
            ).fetchone()
            custom_row = conn.execute(
                "SELECT must_change_password, token_version FROM users WHERE id = ?",
                ("legacy-custom-user",),
            ).fetchone()
            assert default_row == (1, 1)
            assert custom_row == (0, 1)
    finally:
        get_settings.cache_clear()

    # Startup seed_initial_data also flags any unflagged account matching a placeholder password
    probe_email = "startup-placeholder-probe@aatm.local"
    async with AsyncSessionLocal() as db:
        db.add(
            User(
                email=probe_email,
                full_name="Placeholder Probe",
                password_hash=hash_password("change-me-immediately"),
                role="operator",
                must_change_password=False,
                token_version=1,
            )
        )
        await db.commit()
        await seed_initial_data(db)
        await db.commit()
        refreshed = (await db.execute(select(User).where(User.email == probe_email))).scalar_one()
        assert refreshed.must_change_password is True


async def test_sandbox_pull_never_unavailable_status_and_compose_hardening(
    client: AsyncClient, admin_headers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Defects 5 & 6: --pull=never, SANDBOX_UNAVAILABLE in health & dashboard, scripts build image, compose worker hardened."""
    monkeypatch.delenv("AATM_SANDBOX_MODE", raising=False)
    policy = SandboxExecutionPolicy.from_env()
    assert policy.sandbox_mode == "docker"
    assert policy.docker_image == "aatm-sandbox:latest"

    ws = (tmp_path / "agent-ws").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "conftest.py").write_text("import pathlib; pathlib.Path('/home/user/AATM/data/aatm.db').read_bytes()\n")

    cmd = build_isolated_command(["pytest", "-q"], ws, sandbox_mode="docker", docker_bin="/usr/bin/docker")
    assert "--pull=never" in cmd
    mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-v"]
    assert mounts == [f"{ws}:/workspace:rw"]
    assert "--network=none" in cmd
    assert "--read-only" in cmd
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    assert "data/aatm.db" not in " ".join(cmd)

    # When Docker CLI is missing in docker mode, /api/health and /dashboard/overview report SANDBOX_UNAVAILABLE
    # and execution never silently falls back to process mode.
    monkeypatch.setenv("AATM_SANDBOX_MODE", "docker")
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    health = await client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["sandbox"]["status"] == "SANDBOX_UNAVAILABLE"
    assert health.json()["sandbox"]["available"] is False
    assert health.json()["sandbox"]["mode"] == "docker"

    overview = await client.get("/api/v1/dashboard/overview", headers=admin_headers)
    assert overview.status_code == 200
    assert overview.json()["sandbox"]["status"] == "SANDBOX_UNAVAILABLE"
    assert overview.json()["sandbox"]["available"] is False

    async with AsyncSessionLocal() as db:
        with pytest.raises(WorkspaceSandboxError, match="docker"):
            await AgentRuntimeExecutor().run_tool(db, "worker-1", ["python3", "-V"], sandbox_mode="docker")

    # Dockerfiles, compose (Defect 6) & installers/start scripts (Defect 5)
    sandbox_df = (ROOT_DIR / "docker" / "sandbox.Dockerfile").read_text(encoding="utf-8")
    assert "pytest" in sandbox_df and "ruff" in sandbox_df
    assert "USER 10001:10001" in sandbox_df

    worker_df = (ROOT_DIR / "docker" / "worker.Dockerfile").read_text(encoding="utf-8")
    assert "requirements.txt" in worker_df
    assert "USER 65534:65534" in worker_df

    compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:${POSTGRES_PORT:-5432}:5432" in compose
    assert "aatm_password" not in compose
    assert "docker/worker.Dockerfile" in compose
    assert "docker.sock" not in compose
    assert "AATM_SANDBOX_MODE: process" in compose
    assert 'AATM_ALLOW_PROCESS_SANDBOX: "true"' in compose

    for script_rel in (
        "scripts/ubuntu/install.sh",
        "scripts/ubuntu/start.sh",
        "scripts/ubuntu/start-worker.sh",
        "scripts/windows/install.ps1",
        "scripts/windows/start.ps1",
        "scripts/windows/start-worker.ps1",
    ):
        text = (ROOT_DIR / script_rel).read_text(encoding="utf-8")
        assert "docker/sandbox.Dockerfile" in text, f"{script_rel} missing sandbox image build"
        assert "aatm-sandbox:latest" in text, f"{script_rel} missing aatm-sandbox:latest"

    install_sh = (ROOT_DIR / "scripts" / "ubuntu" / "install.sh").read_text(encoding="utf-8")
    assert "secrets.token_hex" in install_sh and "chmod 600 .env" in install_sh
    install_ps1 = (ROOT_DIR / "scripts" / "windows" / "install.ps1").read_text(encoding="utf-8")
    assert "secrets.token_hex" in install_ps1
