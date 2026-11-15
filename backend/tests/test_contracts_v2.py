import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from alembic import command
from app.browser.manager import browser_manager
from app.core.config import get_settings
from app.db.base import Base, utcnow
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import AIProvider, ProviderUsageLog
from app.providers.base import ProviderResponse
from app.providers.router import provider_router
from app.runtime.device_auth import issue_device_token, verify_device_token
from app.schemas.common import (
    GitHubIssueCreateRequest,
    GitHubPRCreateRequest,
    ProviderTestRequest,
    validate_credential_ref,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = BACKEND_ROOT.parent / "contracts" / "schemas.json"
ALEMBIC_CONFIG_PATH = BACKEND_ROOT / "alembic.ini"


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
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _pattern_values(openapi: dict, component: str, field: str) -> set[str]:
    pattern = openapi["components"]["schemas"][component]["properties"][field]["pattern"]
    assert pattern.startswith("^(") and pattern.endswith(")$")
    return set(pattern[2:-2].split("|"))


async def test_contract_v2_matches_openapi_routes_and_enums(client: AsyncClient):
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["version"] == "2.0.0"
    assert contract["api_prefix"] == "/api/v1"

    response = await client.get("/openapi.json")
    assert response.status_code == 200
    openapi = response.json()
    paths = openapi["paths"]
    api_paths = {path for path in paths if path.startswith("/api/")}
    assert api_paths
    assert "/api/health" in api_paths
    assert all(path == "/api/health" or path.startswith(f"{contract['api_prefix']}/") for path in api_paths)
    assert {
        "/api/v1/auth/login",
        "/api/v1/tasks",
        "/api/v1/providers",
        "/api/v1/browsers/{profile_id}/navigate",
        "/api/v1/devices",
        "/api/v1/github/repos",
    }.issubset(paths)
    assert all(methods for methods in paths.values())

    enums = contract["enums"]
    expected_v2_enums = {
        "memory_scopes": ["employee", "team", "organization", "global", "project", "agent", "task", "decision"],
        "execution_modes": ["manual", "automatic", "collaborative"],
        "autonomy_modes": ["supervised", "semi_autonomous", "highly_autonomous"],
        "allocation_modes": ["one_per_employee", "shared", "hybrid"],
        "browser_engines": ["playwright", "browser_use", "openbrowser", "automa"],
        "device_statuses": ["online", "degraded", "offline"],
        "message_types": ["task_assignment", "handoff", "status_update", "direct", "group"],
    }
    for enum_name, expected_values in expected_v2_enums.items():
        assert enums[enum_name] == expected_values

    openapi_enum_fields = {
        "approval_types": ("ApprovalCreate", "action_type"),
        "allocation_modes": ("EmployeeCreate", "allocation_mode"),
        "autonomy_modes": ("EmployeeCreate", "autonomy_mode"),
        "browser_modes": ("BrowserLaunchRequest", "mode"),
        "execution_modes": ("TaskCreate", "execution_mode"),
        "memory_scopes": ("MemoryCreate", "scope"),
        "provider_types": ("ProviderCreateOrUpdate", "provider_type"),
        "task_priority": ("TaskCreate", "priority"),
    }
    for enum_name, (component, field) in openapi_enum_fields.items():
        assert _pattern_values(openapi, component, field) == set(enums[enum_name])

    browser_engines = await client.get("/api/v1/browsers/engines")
    assert browser_engines.status_code == 200
    assert set(browser_engines.json()) == set(enums["browser_engines"])


def test_credential_ref_validation_is_strict_and_does_not_echo_values():
    assert validate_credential_ref(None) is None
    assert validate_credential_ref("env:API_TOKEN_2") == "env:API_TOKEN_2"

    inline_secret = "sk_live_do_not_echo_9f2c"
    for value in (inline_secret, "env:lowercase", "env:BAD-NAME", "env:"):
        with pytest.raises(ValueError) as exc_info:
            validate_credential_ref(value)
        assert str(exc_info.value) == (
            "credential_ref must reference an environment variable (env:NAME); inline credentials are not accepted."
        )
        assert inline_secret not in str(exc_info.value)


async def test_provider_rejects_inline_credentials_without_echoing_secret(client: AsyncClient, auth_headers):
    secret = "sk_live_" + "inline_secret_never_echo_4c91"
    response = await client.post(
        "/api/v1/providers",
        headers=auth_headers,
        json={
            "id": "inline-secret-provider",
            "name": "Inline Secret Provider",
            "provider_type": "anthropic_claude",
            "credential_ref": secret,
            "default_model": "claude-test-model",
        },
    )

    assert response.status_code == 400
    assert secret not in response.text
    assert response.json()["detail"] == (
        "credential_ref must reference an environment variable (env:NAME); inline credentials are not accepted."
    )
    providers = await client.get("/api/v1/providers")
    assert all(provider["id"] != "inline-secret-provider" for provider in providers.json())


async def test_browser_navigation_maps_value_error_to_http_400(client: AsyncClient, auth_headers, monkeypatch):
    async def reject_disallowed_scheme(db, profile, url, title=None):
        raise ValueError("Disallowed URL scheme")

    monkeypatch.setattr(browser_manager, "navigate_profile", reject_disallowed_scheme)
    response = await client.post(
        "/api/v1/browsers/profile-frontend-preview/navigate",
        headers=auth_headers,
        json={"url": "javascript:alert(1)"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Disallowed URL scheme"


def test_github_write_schemas_require_approval_and_validate_payloads():
    pull_request = GitHubPRCreateRequest(
        approval_id="approval-pr-001",
        repo="example/project",
        title="Add validated pull request payloads",
        body="Contract coverage",
        head="feature/contracts",
        base="main",
        draft=True,
    )
    issue = GitHubIssueCreateRequest(
        approval_id="approval-issue-001",
        repo="example/project",
        title="Track integration follow-up",
        body="Wire the approved GitHub write endpoint.",
        labels=["backend", "approved"],
        assignees=["worker-4"],
    )

    assert pull_request.approval_id == "approval-pr-001"
    assert pull_request.repo == "example/project"
    assert pull_request.draft is True
    assert issue.approval_id == "approval-issue-001"
    assert issue.labels == ["backend", "approved"]
    assert "approval_id" in GitHubPRCreateRequest.model_fields
    assert "approval_id" in GitHubIssueCreateRequest.model_fields

    with pytest.raises(ValidationError):
        GitHubPRCreateRequest(
            repo="example/project",
            title="Missing approval ID",
            head="feature/no-approval",
        )
    with pytest.raises(ValidationError):
        GitHubIssueCreateRequest(
            approval_id="approval-issue-invalid-repo",
            repo="project-without-owner",
            title="Invalid repository",
        )


async def test_operator_jwt_device_heartbeat_and_userscript_role_scoping(client: AsyncClient, auth_headers):
    dev = await client.post(
        "/api/v1/devices",
        headers=auth_headers,
        json={
            "id": "node-ui-hb-01",
            "name": "UI Heartbeat Node",
            "device_type": "remote_server",
            "host_address": "10.0.0.44",
            "os_name": "Ubuntu 24.04",
        },
    )
    assert dev.status_code == 201

    hb = await client.post(
        "/api/v1/devices/node-ui-hb-01/heartbeat",
        headers=auth_headers,
        json={"cpu_percent": 21, "ram_percent": 34},
    )
    assert hb.status_code == 200
    assert hb.json()["status"] == "online"

    bad_us = await client.post(
        "/api/v1/userscripts",
        headers=auth_headers,
        json={
            "name": "Wildcard Role Script",
            "description": "Should be rejected by role policy",
            "match_patterns": ["https://example.com/*"],
            "allowed_roles": ["*"],
            "code": "console.log('ok');",
        },
    )
    assert bad_us.status_code == 400


async def test_github_write_endpoints_enforce_approval_gate(client: AsyncClient, auth_headers, monkeypatch):
    import subprocess

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/oldman-088/AATM/pull/99\n", stderr="")

    monkeypatch.setattr("app.github_client.client.subprocess.run", fake_run)

    # Create pending approval (not yet approved) -> must return 403
    pending_app = await client.post(
        "/api/v1/approvals",
        headers=auth_headers,
        json={
            "action_type": "merge",
            "title": "Create integration PR",
            "description": "Test approval gate on POST /api/v1/github/prs",
        },
    )
    assert pending_app.status_code == 201
    app_id = pending_app.json()["id"]

    blocked = await client.post(
        "/api/v1/github/prs",
        headers=auth_headers,
        json={
            "approval_id": app_id,
            "repo": "oldman-088/AATM",
            "title": "Blocked PR",
            "head": "feature/test",
            "base": "main",
        },
    )
    assert blocked.status_code == 403

    dec = await client.post(
        f"/api/v1/approvals/{app_id}/decide",
        headers=auth_headers,
        json={"decision": "approved", "review_note": "Approved for test"},
    )
    assert dec.status_code == 200

    allowed = await client.post(
        "/api/v1/github/prs",
        headers=auth_headers,
        json={
            "approval_id": app_id,
            "repo": "oldman-088/AATM",
            "title": "Approved PR",
            "head": "feature/test",
            "base": "main",
        },
    )
    assert allowed.status_code == 201
    assert allowed.json()["ok"] is True
    assert allowed.json()["allowed"] is True

    issue = await client.post(
        "/api/v1/github/issues",
        headers=auth_headers,
        json={
            "approval_id": app_id,
            "repo": "oldman-088/AATM",
            "title": "Approved Issue",
            "body": "Created via approval-gated endpoint",
            "labels": ["enhancement"],
        },
    )
    assert issue.status_code == 201
    assert issue.json()["ok"] is True


async def test_device_token_revocation_invalidates_old_token_and_allows_rotated_token(
    client: AsyncClient, auth_headers
):
    device_id = "token-revoke-v2-test"
    register_payload = {
        "name": "Revocation Test Device",
        "device_type": "remote_server",
        "host_address": "127.0.0.2",
        "os_name": "Linux",
    }
    registered = await client.post(
        f"/api/v1/devices/{device_id}/register",
        headers=auth_headers,
        json=register_payload,
    )
    assert registered.status_code == 200
    old_token = registered.json()["device_token"]
    old_headers = {"X-AATM-Device-Token": old_token}

    assert issue_device_token(device_id, 1) == old_token
    assert verify_device_token(device_id, old_token, token_version=1) is True
    assert verify_device_token(device_id, old_token, token_version=2) is False
    status_url = f"/api/v1/devices/{device_id}/status"
    assert (await client.get(status_url, headers=old_headers)).status_code == 200

    unauthenticated_revoke = await client.post(f"/api/v1/devices/{device_id}/revoke")
    assert unauthenticated_revoke.status_code == 401

    revoked = await client.post(f"/api/v1/devices/{device_id}/revoke", headers=auth_headers)
    assert revoked.status_code == 200
    assert revoked.json()["token_version"] == 2
    assert revoked.json()["revoked"] is True
    assert (await client.get(status_url, headers=old_headers)).status_code == 401

    new_token = revoked.json()["device_token"]
    assert new_token != old_token
    assert new_token == issue_device_token(device_id, 2)
    assert (await client.get(status_url, headers={"X-AATM-Device-Token": new_token})).status_code == 200


def _upgrade_sqlite_database(database_path: Path, revision: str, monkeypatch) -> None:
    sync_url = f"sqlite:///{database_path.as_posix()}"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("SYNC_DATABASE_URL", sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    try:
        command.upgrade(config, revision)
    finally:
        get_settings.cache_clear()


def test_token_version_migration_supports_fresh_and_existing_sqlite_databases(tmp_path, monkeypatch):
    fresh_db = tmp_path / "fresh.sqlite"
    _upgrade_sqlite_database(fresh_db, "head", monkeypatch)
    with sqlite3.connect(fresh_db) as connection:
        fresh_columns = {row[1]: row for row in connection.execute("PRAGMA table_info(execution_devices)")}
        assert "token_version" in fresh_columns
        assert fresh_columns["token_version"][2].upper() == "INTEGER"
        assert fresh_columns["token_version"][3] == 1
        assert str(fresh_columns["token_version"][4]).strip("'\"") == "1"

    existing_db = tmp_path / "existing.sqlite"
    _upgrade_sqlite_database(existing_db, "ee92483cefa3", monkeypatch)
    with sqlite3.connect(existing_db) as connection:
        connection.execute(
            """
            INSERT INTO execution_devices (
                id, name, device_type, host_address, os_name, cpu_cores, ram_gb,
                capabilities, status, max_concurrent_tasks, active_tasks, last_heartbeat, registered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "existing-device",
                "Existing Device",
                "remote_server",
                "127.0.0.3",
                "Linux",
                2,
                4.0,
                "[]",
                "online",
                2,
                0,
                "2026-11-15 00:00:00",
                "2026-11-15 00:00:00",
            ),
        )
        connection.commit()

    _upgrade_sqlite_database(existing_db, "head", monkeypatch)
    with sqlite3.connect(existing_db) as connection:
        row = connection.execute(
            "SELECT token_version FROM execution_devices WHERE id = ?", ("existing-device",)
        ).fetchone()
        assert row == (1,)


async def test_provider_usage_includes_budget_headroom(client: AsyncClient):
    provider_id = "budget-summary-v2-test"
    async with AsyncSessionLocal() as db:
        db.add(
            AIProvider(
                id=provider_id,
                name="Budget Summary Test Provider",
                provider_type="arena_agent",
                default_model="budget-test-model",
                available_models=["budget-test-model"],
                daily_limit_usd=10.0,
                config={"monthly_limit_usd": 8.0},
            )
        )
        db.add(
            ProviderUsageLog(
                provider_id=provider_id,
                model_name="budget-test-model",
                input_tokens=10,
                output_tokens=5,
                estimated_cost_usd=2.5,
                latency_ms=1.0,
                success=True,
                created_at=utcnow(),
            )
        )
        await db.commit()

    response = await client.get("/api/v1/providers/usage")
    assert response.status_code == 200
    budget = response.json()["budget_summary"]
    assert budget["global_daily_spend_usd"] >= 2.5
    assert budget["global_monthly_spend_usd"] >= 2.5
    assert budget["daily_remaining_usd"] == pytest.approx(
        max(0.0, budget["daily_limit_usd"] - budget["global_daily_spend_usd"])
    )
    assert budget["monthly_remaining_usd"] == pytest.approx(
        max(0.0, budget["monthly_limit_usd"] - budget["global_monthly_spend_usd"])
    )

    provider_budget = budget["per_provider"][provider_id]
    assert provider_budget["daily_spend_usd"] == pytest.approx(2.5)
    assert provider_budget["monthly_spend_usd"] == pytest.approx(2.5)
    assert provider_budget["daily_remaining_usd"] == pytest.approx(7.5)
    assert provider_budget["monthly_limit_usd"] == pytest.approx(8.0)
    assert provider_budget["monthly_remaining_usd"] == pytest.approx(5.5)


async def test_provider_test_forwards_agent_and_session_context(client: AsyncClient, auth_headers, monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_route_and_execute(db, request, preferred_provider_id=None):
        captured["request"] = request
        captured["preferred_provider_id"] = preferred_provider_id
        return ProviderResponse(
            provider_id=preferred_provider_id or "arena-agent",
            model="test-model",
            content="ok",
            input_tokens=1,
            output_tokens=1,
            estimated_cost_usd=0.0,
            latency_ms=1.0,
        )

    monkeypatch.setattr(provider_router, "route_and_execute", fake_route_and_execute)
    response = await client.post(
        "/api/v1/providers/arena-agent/test",
        headers=auth_headers,
        json={"dry_run": True, "agent_id": "worker-1", "session_id": "provider-session-v2-test"},
    )

    assert response.status_code == 200
    request = captured["request"]
    assert request.agent_id == "worker-1"
    assert request.metadata["session_id"] == "provider-session-v2-test"
    assert captured["preferred_provider_id"] == "arena-agent"
    assert ProviderTestRequest(agent_id="worker-1", session_id="session-1").agent_id == "worker-1"
