import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.browser.manager import browser_manager
from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.schemas.common import (
    GitHubIssueCreateRequest,
    GitHubPRCreateRequest,
    validate_credential_ref,
)

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "schemas.json"


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
    secret = "sk_live_inline_secret_never_echo_4c91"
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
