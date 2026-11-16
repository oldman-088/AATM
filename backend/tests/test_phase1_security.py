"""Phase 1 — Security Hardening verification suite."""

import json
import os
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main_module
from app.core.config import ROOT_DIR, Settings, get_settings, validate_runtime_security
from app.core.events import event_bus
from app.core.rate_limit import login_rate_limiter
from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import AuditLog, Project, User
from app.runtime.sandbox import SandboxExecutionPolicy, build_isolated_command


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
    settings = get_settings()
    login_rate_limiter.reset()
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _concrete_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "nonexistent-id", path)


async def test_all_non_public_get_routes_return_401_without_auth(client: AsyncClient):
    public_get_paths = {"/api/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    checked: list[str] = []

    def _iter_api_routes(routes, prefix: str = ""):
        for route in routes:
            if isinstance(route, APIRoute):
                yield f"{prefix}{route.path}", route.methods or set()
            elif hasattr(route, "effective_route_contexts"):
                for ctx in route.effective_route_contexts():
                    yield f"{prefix}{ctx.path}", ctx.methods or set()

    discovered = list(_iter_api_routes(app.routes))
    openapi_gets = {
        path for path, methods in app.openapi()["paths"].items() if "get" in methods and path.startswith("/api")
    }
    discovered_gets = {p for p, m in discovered if "GET" in m and p.startswith("/api")}
    assert openapi_gets.issubset(discovered_gets)

    for full_path in sorted(discovered_gets):
        if full_path in public_get_paths:
            continue
        resp = await client.get(_concrete_path(full_path))
        assert resp.status_code == 401, f"GET {full_path} returned {resp.status_code} instead of 401"
        checked.append(full_path)

    assert "/api/v1/dashboard/overview" in checked
    assert "/api/v1/projects" in checked
    assert "/api/v1/git/status" in checked
    assert "/api/v1/devices" in checked
    assert len(checked) >= 25


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
                )
            )
            await db.commit()

    viewer_headers = {"Authorization": f"Bearer {create_access_token(subject=viewer_email, role='viewer')}"}

    # Viewer can read protected GET endpoints
    read_resp = await client.get("/api/v1/dashboard/overview", headers=viewer_headers)
    assert read_resp.status_code == 200

    # Viewer is rejected (403) on operator/admin write routes
    for method, path, payload in (
        ("POST", "/api/v1/projects", {"name": "Blocked Viewer Project"}),
        ("POST", "/api/v1/providers", {"name": "Blocked Provider", "provider_type": "openai_compatible", "default_model": "gpt-4o"}),
        ("POST", "/api/v1/approvals", {"action_type": "merge", "title": "Blocked Approval"}),
        ("POST", "/api/v1/orchestration/cycle", None),
        ("POST", "/api/v1/emergency-stop", {"scope": "global", "reason": "test"}),
    ):
        resp = await client.request(method, path, headers=viewer_headers, json=payload)
        assert resp.status_code == 403, f"{method} {path} allowed viewer with {resp.status_code}"


async def test_git_status_rejects_unregistered_repo_path(client: AsyncClient, admin_headers, tmp_path: Path):
    ok_default = await client.get("/api/v1/git/status", headers=admin_headers)
    assert ok_default.status_code == 200

    for bad_path in ("/etc", "/tmp", str(tmp_path), "../../etc"):
        denied = await client.get("/api/v1/git/status", params={"repo_path": bad_path}, headers=admin_headers)
        assert denied.status_code == 403, f"Expected 403 for {bad_path}, got {denied.status_code}"

    # Registering a project makes its repo_path explicitly allowed
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


def _disable_background_lifespan_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.runtime.background_scheduler import BackgroundScheduler

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(BackgroundScheduler, "run_orchestration_forever", _noop)
    monkeypatch.setattr(BackgroundScheduler, "run_self_healing_forever", _noop)
    monkeypatch.setattr(get_settings(), "AGENT_LOOP_ENABLED", False)


def test_websocket_requires_jwt_and_rejects_unauthenticated_within_timeout(monkeypatch: pytest.MonkeyPatch):
    _disable_background_lifespan_loops(monkeypatch)
    monkeypatch.setattr(main_module, "WS_AUTH_TIMEOUT_SECONDS", 0.15)
    settings = get_settings()
    token = create_access_token(subject=settings.DEFAULT_ADMIN_EMAIL, role="admin")

    with TestClient(app) as sync_client:
        # 1. Missing token times out quickly and closes with 4401
        with sync_client.websocket_connect("/ws/events") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4401

        # 2. Invalid token closes immediately with 4401
        with sync_client.websocket_connect("/ws/events") as ws:
            ws.send_text(json.dumps({"token": "invalid.jwt.token"}))
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4401

        # 3. Valid token via first message authenticates
        with sync_client.websocket_connect("/ws/events") as ws:
            ws.send_text(json.dumps({"token": token}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "auth.ok"

        # 4. Valid token via query parameter also succeeds
        with sync_client.websocket_connect(f"/ws/events?token={token}") as ws:
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "auth.ok"


async def test_websocket_emits_only_to_authenticated_sockets(monkeypatch: pytest.MonkeyPatch):
    _disable_background_lifespan_loops(monkeypatch)
    monkeypatch.setattr(main_module, "WS_AUTH_TIMEOUT_SECONDS", 0.15)
    settings = get_settings()
    token = create_access_token(subject=settings.DEFAULT_ADMIN_EMAIL, role="admin")

    with TestClient(app) as sync_client:
        with sync_client.websocket_connect(f"/ws/events?token={token}") as ws:
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
    assert defaults.AATM_SANDBOX_MODE in {"docker", os.environ.get("AATM_SANDBOX_MODE", "docker")}

    csv_cors = Settings(CORS_ORIGINS="https://app.aatm.ai, https://ops.aatm.ai")
    assert csv_cors.get_cors_origins() == ["https://app.aatm.ai", "https://ops.aatm.ai"]

    # Default SECRET_KEY in production must fail
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_runtime_security(
            Settings(
                ENVIRONMENT="production",
                DEFAULT_ADMIN_PASSWORD="Strong-Admin-Password-2026!",
                AATM_SANDBOX_MODE="docker",
            )
        )

    # Short SECRET_KEY in production must fail
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_runtime_security(
            Settings(
                ENVIRONMENT="production",
                SECRET_KEY="too-short",
                DEFAULT_ADMIN_PASSWORD="Strong-Admin-Password-2026!",
                AATM_SANDBOX_MODE="docker",
            )
        )

    # Default admin password in production must fail
    with pytest.raises(RuntimeError, match="DEFAULT_ADMIN_PASSWORD"):
        validate_runtime_security(
            Settings(
                ENVIRONMENT="production",
                SECRET_KEY="a" * 64,
                DEFAULT_ADMIN_PASSWORD="aatm-admin-2026",
                AATM_SANDBOX_MODE="docker",
            )
        )

    # Un-exempted process sandbox in production must fail
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

    # Hardened production settings pass
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


async def test_login_rate_limiting_and_failed_audit(client: AsyncClient):
    login_rate_limiter.reset()
    target_email = "rate-limit-check@aatm.local"

    for idx in range(5):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": target_email, "password": f"wrong-password-{idx}"},
            headers={"X-Forwarded-For": "203.0.113.42"},
        )
        assert resp.status_code == 401

    blocked = await client.post(
        "/api/v1/auth/login",
        json={"email": target_email, "password": "wrong-password-final"},
        headers={"X-Forwarded-For": "203.0.113.42"},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1

    async with AsyncSessionLocal() as db:
        audits = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.actor == target_email,
                    AuditLog.action.in_(["auth.login_failed", "auth.login_rate_limited"]),
                )
            )
        ).scalars().all()
    actions = [a.action for a in audits]
    assert actions.count("auth.login_failed") == 5
    assert "auth.login_rate_limited" in actions
    login_rate_limiter.reset()


async def test_first_login_must_change_password_and_rotate(client: AsyncClient):
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

    bad_cur = await client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": "wrong", "new_password": "NewStrongPassword!2026"},
    )
    assert bad_cur.status_code == 401

    rotated_pw = "RotatedAdminPassword!2026"
    ok = await client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": settings.DEFAULT_ADMIN_PASSWORD, "new_password": rotated_pw},
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["must_change_password"] is False

    # Restore original password so downstream module tests aren't affected if re-ordered
    new_login = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": rotated_pw},
    )
    assert new_login.status_code == 200
    assert new_login.json()["user"]["must_change_password"] is False

    # Default placeholder password cannot be chosen as the new password
    reject_default = await client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {new_login.json()['access_token']}"},
        json={"current_password": rotated_pw, "new_password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert reject_default.status_code == 400

    # Restore seeded state directly in DB so any later test fixture sees clean defaults
    async with AsyncSessionLocal() as db:
        admin = (await db.execute(select(User).where(User.email == settings.DEFAULT_ADMIN_EMAIL))).scalar_one()
        admin.password_hash = hash_password(settings.DEFAULT_ADMIN_PASSWORD)
        admin.must_change_password = True
        await db.commit()


def test_docker_sandbox_isolation_blocks_host_db_and_hardens_infra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Policy defaults to docker mode when AATM_SANDBOX_MODE is unset
    monkeypatch.delenv("AATM_SANDBOX_MODE", raising=False)
    policy = SandboxExecutionPolicy.from_env()
    assert policy.sandbox_mode == "docker"
    assert policy.docker_image == "aatm-sandbox:latest"

    # An agent-written conftest.py inside workspace cannot see host data/aatm.db in docker mode
    ws = (tmp_path / "agent-ws").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "conftest.py").write_text("import pathlib; pathlib.Path('/home/user/AATM/data/aatm.db').read_bytes()\n")

    cmd = build_isolated_command(["pytest", "-q"], ws, sandbox_mode="docker", docker_bin="/usr/bin/docker")
    mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-v"]
    assert mounts == [f"{ws}:/workspace:rw"]
    assert "--network=none" in cmd
    assert "--read-only" in cmd
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    assert "data/aatm.db" not in " ".join(cmd)

    # Dockerfiles, compose & installers meet Phase 1 hardening requirements
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

    install_sh = (ROOT_DIR / "scripts" / "ubuntu" / "install.sh").read_text(encoding="utf-8")
    assert "secrets.token_hex" in install_sh and "chmod 600 .env" in install_sh
    install_ps1 = (ROOT_DIR / "scripts" / "windows" / "install.ps1").read_text(encoding="utf-8")
    assert "secrets.token_hex" in install_ps1
