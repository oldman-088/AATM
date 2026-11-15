import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from alembic import command
from app.browser.manager import browser_manager
from app.core.config import get_settings
from app.core.events import event_bus
from app.core.security import create_access_token, hash_password
from app.db.base import Base, utcnow
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import (
    Agent,
    AgentMessage,
    AIProvider,
    AuditLog,
    ExecutionDevice,
    Project,
    ProviderUsageLog,
    Task,
    User,
    WorkspaceArtifact,
)
from app.providers.base import ProviderResponse
from app.providers.router import provider_router
from app.runtime.device_auth import issue_device_token, verify_device_token
from app.schemas.common import (
    DeviceTokenRevokeResponse,
    GitHubBranchCreateRequest,
    GitHubIssueCreateRequest,
    GitHubPRCreateRequest,
    GitWorktreeCreateRequest,
    ProviderTestRequest,
    ProviderUsageResponse,
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


async def _headers_for_role(role: str, scope: str = "device-revoke") -> dict[str, str]:
    email = f"round2-{role}-{scope}@example.test"
    async with AsyncSessionLocal() as db:
        db.add(
            User(
                email=email,
                full_name=f"Round 2 {role.title()}",
                password_hash=hash_password("test-only-password"),
                role=role,
            )
        )
        await db.commit()
    token = create_access_token(subject=email, role=role)
    return {"Authorization": f"Bearer {token}"}


def _pattern_values(openapi: dict, component: str, field: str) -> set[str]:
    pattern = openapi["components"]["schemas"][component]["properties"][field]["pattern"]
    assert pattern.startswith("^(") and pattern.endswith(")$")
    return set(pattern[2:-2].split("|"))


async def test_contract_v2_matches_openapi_routes_and_enums(client: AsyncClient):
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["version"] == "2.2.0"
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
        "/api/v1/devices/{device_id}/revoke",
        "/api/v1/github/repos",
        "/api/v1/github/branches",
        "/api/v1/traces",
        "/api/v1/providers/usage",
        "/api/v1/providers/{provider_id}/test",
        "/api/v1/agents/{agent_id}",
        "/api/v1/employees",
        "/api/v1/git/worktrees",
        "/api/v1/git/worktrees/{agent_id}",
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
    assert "branch" in enums["approval_types"]

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

    operations = contract["operations"]
    assert operations["device_token_revoke"]["operator_roles"] == ["admin", "operator"]
    revoke_schema = operations["device_token_revoke"]["response"]
    assert revoke_schema["required"] == ["id", "token_version", "device_token"]
    assert revoke_schema["additionalProperties"] is False
    assert set(operations["providers_usage"]["response"]["required"]) == {
        "budget_summary",
        "summary",
        "logs",
    }
    assert set(operations["providers_usage"]["response"]["properties"]["budget_summary"]["required"]) == {
        "global_daily_spend_usd",
        "global_monthly_spend_usd",
        "daily_limit_usd",
        "monthly_limit_usd",
        "daily_remaining_usd",
        "monthly_remaining_usd",
    }
    provider_test_request = operations["provider_test"]["request"]
    assert provider_test_request["required"] == []
    assert provider_test_request["properties"]["agent_id"] == {
        "type": ["string", "null"],
        "default": None,
    }
    assert provider_test_request["properties"]["session_id"] == {
        "type": ["string", "null"],
        "default": None,
    }
    trace_operation = operations["execution_traces"]
    assert trace_operation["path"] == "/api/v1/traces"
    assert set(trace_operation["query"]) == {"agent_id", "project_id", "limit"}
    trace_sources = trace_operation["response"]["properties"]["traces"]["items"]["properties"]["source"]["enum"]
    assert set(trace_sources) == {"audit_log", "provider_usage_log", "workspace_artifact", "agent_message"}
    assert {"agent_id", "project_id"}.issubset(trace_operation["query"])

    reports_to_schema = contract["entities"]["agent"]["properties"]["reports_to"]
    assert reports_to_schema == {"type": ["string", "null"], "maxLength": 64}
    assert contract["entities"]["agent"]["non_manager_default_reports_to"] == "manager-0"
    assert "reports_to" in operations["employee_create"]["request"]["properties"]
    assert "reports_to" in operations["agent_update"]["request"]["properties"]

    create_worktree = operations["git_worktree_create"]
    assert create_worktree["path"] == "/api/v1/git/worktrees"
    assert create_worktree["operator_roles"] == ["admin", "operator"]
    assert create_worktree["request"]["required"] == ["agent_id", "branch_name"]
    assert create_worktree["request"]["properties"]["base_ref"]["default"] == "HEAD"
    assert create_worktree["request"]["properties"]["approval_id"]["default"] is None
    assert set(create_worktree["error_responses"]) == {"400", "403"}
    remove_worktree = operations["git_worktree_remove"]
    assert remove_worktree["path"] == "/api/v1/git/worktrees/{agent_id}"
    assert remove_worktree["operator_roles"] == ["admin", "operator"]
    assert set(remove_worktree["error_responses"]) == {"400", "403"}

    expected_github_requests = {
        "github_branch_create": {"approval_id", "repo", "branch_name", "from_branch"},
        "github_pr_create": {"approval_id", "repo", "title", "body", "head", "base", "draft"},
        "github_issue_create": {"approval_id", "repo", "title", "body", "labels", "assignees"},
    }
    for operation_name, expected_fields in expected_github_requests.items():
        operation_request = operations[operation_name]["request"]
        assert "approval_id" in operation_request["required"]
        assert set(operation_request["properties"]) == expected_fields

    revoke_schema_in_openapi = openapi["components"]["schemas"]["DeviceTokenRevokeResponse"]
    assert set(revoke_schema_in_openapi["required"]) == {"id", "token_version", "device_token"}
    assert revoke_schema_in_openapi["additionalProperties"] is False
    usage_schema_in_openapi = openapi["components"]["schemas"]["ProviderUsageResponse"]
    assert "budget_summary" in usage_schema_in_openapi["properties"]
    provider_test_schema = openapi["components"]["schemas"]["ProviderTestRequest"]["properties"]
    assert provider_test_schema["agent_id"].get("anyOf") or provider_test_schema["agent_id"].get("type") == "string"
    assert provider_test_schema["session_id"].get("anyOf") or provider_test_schema["session_id"].get("type") == "string"
    assert "branch" in _pattern_values(openapi, "ApprovalCreate", "action_type")
    assert "reports_to" in openapi["components"]["schemas"]["AgentUpdate"]["properties"]
    assert "reports_to" in openapi["components"]["schemas"]["EmployeeCreate"]["properties"]
    worktree_schema = openapi["components"]["schemas"]["GitWorktreeCreateRequest"]["properties"]
    assert worktree_schema["base_ref"]["default"] == "HEAD"
    assert set(paths["/api/v1/git/worktrees"]) == {"post"}
    assert set(paths["/api/v1/git/worktrees/{agent_id}"]) == {"delete"}

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
    branch = GitHubBranchCreateRequest(
        approval_id="approval-branch-001",
        repo="example/project",
        branch_name="feature/contracts",
        from_branch="main",
    )
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

    assert branch.approval_id == "approval-branch-001"
    assert branch.repo == "example/project"
    assert branch.branch_name == "feature/contracts"
    assert branch.from_branch == "main"
    assert pull_request.approval_id == "approval-pr-001"
    assert pull_request.repo == "example/project"
    assert pull_request.draft is True
    assert issue.approval_id == "approval-issue-001"
    assert issue.labels == ["backend", "approved"]
    assert set(GitHubBranchCreateRequest.model_fields) == {
        "approval_id",
        "repo",
        "branch_name",
        "from_branch",
    }
    assert set(GitHubPRCreateRequest.model_fields) == {
        "approval_id",
        "repo",
        "title",
        "body",
        "head",
        "base",
        "draft",
    }
    assert set(GitHubIssueCreateRequest.model_fields) == {
        "approval_id",
        "repo",
        "title",
        "body",
        "labels",
        "assignees",
    }

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

    worktree = GitWorktreeCreateRequest(agent_id="worker-1", branch_name="feature/worktree")
    assert worktree.base_ref == "HEAD"
    assert worktree.approval_id is None
    assert set(GitWorktreeCreateRequest.model_fields) == {
        "agent_id",
        "branch_name",
        "base_ref",
        "approval_id",
    }
    with pytest.raises(ValidationError):
        GitWorktreeCreateRequest(agent_id="", branch_name="feature/worktree")


async def test_reports_to_update_serialization_and_employee_default(client: AsyncClient, auth_headers, monkeypatch):
    from app.api.routes import platform as platform_routes

    agent_id = "round3-reports-to-patch-agent"
    async with AsyncSessionLocal() as db:
        db.add(
            Agent(
                id=agent_id,
                name="Reports To Patch Agent",
                role="worker",
                specialty="contract test",
            )
        )
        await db.commit()

    updated = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"reports_to": "manager-7"},
    )
    assert updated.status_code == 200
    assert updated.json()["reports_to"] == "manager-7"

    cleared = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"reports_to": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["reports_to"] is None

    created_ids = iter(("round3-employee-default", "round3-employee-explicit"))

    async def fake_create_dynamic_employee(*, db, name, **kwargs):
        employee = Agent(
            id=next(created_ids),
            name=name,
            role="worker",
            specialty="contract test",
        )
        db.add(employee)
        await db.flush()
        return employee

    monkeypatch.setattr(platform_routes, "create_dynamic_employee", fake_create_dynamic_employee)
    defaulted = await client.post(
        "/api/v1/employees",
        headers=auth_headers,
        json={"name": "Round Three Default Reports"},
    )
    assert defaulted.status_code == 201
    assert defaulted.json()["reports_to"] == "manager-0"

    explicit = await client.post(
        "/api/v1/employees",
        headers=auth_headers,
        json={"name": "Round Three Explicit Reports", "reports_to": "manager-7"},
    )
    assert explicit.status_code == 201
    assert explicit.json()["reports_to"] == "manager-7"


async def test_git_worktree_endpoints_require_operator_and_map_errors(
    client: AsyncClient, auth_headers, monkeypatch
):
    from app.git_workflows.manager import git_workflow_manager

    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_create_agent_worktree(**kwargs):
        calls.append(("create", kwargs))
        branch_name = kwargs["branch_name"]
        if branch_name == "bad-branch":
            return {"ok": False, "allowed": False, "error": "Invalid branch name."}
        if branch_name == "protected-branch":
            return {
                "ok": False,
                "allowed": False,
                "is_protected_target": True,
                "error": "Target branch is protected; approval is required.",
            }
        if kwargs["agent_id"] == "../outside":
            return {"ok": False, "allowed": False, "error": "Invalid agent ID for worktree containment."}
        return {
            "ok": True,
            "allowed": True,
            "agent_id": kwargs["agent_id"],
            "branch_name": branch_name,
            "base_ref": kwargs["base_ref"],
            "worktree_path": "/tmp/worktrees/worker-4",
        }

    async def fake_remove_agent_worktree(**kwargs):
        calls.append(("remove", kwargs))
        if kwargs["agent_id"] == "bad_agent!":
            return {"ok": False, "allowed": False, "error": "Invalid agent ID for worktree containment."}
        return {"ok": True, "allowed": True, "agent_id": kwargs["agent_id"], "removed": True}

    monkeypatch.setattr(git_workflow_manager, "create_agent_worktree", fake_create_agent_worktree)
    monkeypatch.setattr(git_workflow_manager, "remove_agent_worktree", fake_remove_agent_worktree)
    operator_headers = await _headers_for_role("operator", "worktree-operator")
    worker_headers = await _headers_for_role("worker", "worktree-operator")

    denied_create = await client.post(
        "/api/v1/git/worktrees",
        headers=worker_headers,
        json={"agent_id": "worker-4", "branch_name": "feature/worker-denied"},
    )
    denied_remove = await client.delete("/api/v1/git/worktrees/worker-4", headers=worker_headers)
    assert denied_create.status_code == 403
    assert denied_remove.status_code == 403
    assert calls == []

    created = await client.post(
        "/api/v1/git/worktrees",
        headers=operator_headers,
        json={"agent_id": "worker-4", "branch_name": "feature/worktree", "base_ref": "main"},
    )
    assert created.status_code == 201
    assert created.json()["ok"] is True
    assert calls[-1][0] == "create"
    assert calls[-1][1]["agent_id"] == "worker-4"
    assert calls[-1][1]["branch_name"] == "feature/worktree"
    assert calls[-1][1]["base_ref"] == "main"
    assert calls[-1][1]["approval"] is None
    assert calls[-1][1]["actor"] == "round2-operator-worktree-operator@example.test"

    removed = await client.delete("/api/v1/git/worktrees/worker-4", headers=operator_headers)
    assert removed.status_code == 200
    assert removed.json()["removed"] is True
    assert calls[-1][0] == "remove"
    assert calls[-1][1]["agent_id"] == "worker-4"

    bad_branch = await client.post(
        "/api/v1/git/worktrees",
        headers=auth_headers,
        json={"agent_id": "worker-4", "branch_name": "bad-branch"},
    )
    assert bad_branch.status_code == 400
    assert bad_branch.json()["detail"] == "Invalid branch name."

    bad_path = await client.post(
        "/api/v1/git/worktrees",
        headers=auth_headers,
        json={"agent_id": "../outside", "branch_name": "feature/path-error"},
    )
    assert bad_path.status_code == 400
    assert "worktree containment" in bad_path.json()["detail"]

    protected = await client.post(
        "/api/v1/git/worktrees",
        headers=auth_headers,
        json={"agent_id": "worker-4", "branch_name": "protected-branch"},
    )
    assert protected.status_code == 403
    assert "approval is required" in protected.json()["detail"]

    invalid_approval = await client.post(
        "/api/v1/git/worktrees",
        headers=auth_headers,
        json={
            "agent_id": "worker-4",
            "branch_name": "feature/invalid-approval",
            "approval_id": "missing-worktree-approval",
        },
    )
    assert invalid_approval.status_code == 403

    bad_remove_path = await client.delete(
        "/api/v1/git/worktrees/bad_agent!",
        headers=auth_headers,
    )
    assert bad_remove_path.status_code == 400
    assert "worktree containment" in bad_remove_path.json()["detail"]


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


async def test_github_branch_creation_requires_matching_approval_and_allowlist(
    client: AsyncClient, auth_headers, monkeypatch
):
    from app.git_workflows.manager import git_workflow_manager

    gh_calls: list[list[str]] = []

    def fake_run_gh(args, cwd=None):
        gh_calls.append(args)
        if any("/git/ref/heads/" in arg for arg in args):
            return 0, "a" * 40
        return 0, '{"ref":"refs/heads/feature/approved-branch"}'

    monkeypatch.setattr(git_workflow_manager.github_client, "_run_gh", fake_run_gh)
    request_body = {
        "repo": "example/project",
        "branch_name": "feature/approved-branch",
        "from_branch": "main",
    }
    pending_approval = await client.post(
        "/api/v1/approvals",
        headers=auth_headers,
        json={
            "action_type": "branch",
            "title": "Create approved feature branch",
            "description": "Test approval-gated branch creation",
            "payload": request_body,
        },
    )
    assert pending_approval.status_code == 201
    approval_id = pending_approval.json()["id"]
    blocked_pending = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={"approval_id": approval_id, **request_body},
    )
    assert blocked_pending.status_code == 403
    assert gh_calls == []

    decision = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        headers=auth_headers,
        json={"decision": "approved", "review_note": "Approved for test"},
    )
    assert decision.status_code == 200
    created = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={"approval_id": approval_id, **request_body},
    )
    assert created.status_code == 201
    assert created.json()["ok"] is True
    assert created.json()["allowed"] is True
    assert created.json()["branch_name"] == request_body["branch_name"]
    assert len(gh_calls) == 2

    mismatched = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={
            "approval_id": approval_id,
            "repo": request_body["repo"],
            "branch_name": "feature/not-approved",
            "from_branch": request_body["from_branch"],
        },
    )
    assert mismatched.status_code == 403
    assert len(gh_calls) == 2

    blocked_repo = "not-allowed/project"
    denied_approval = await client.post(
        "/api/v1/approvals",
        headers=auth_headers,
        json={
            "action_type": "branch",
            "title": "Disallowed repository branch",
            "description": "Must be denied by repository policy",
            "payload": {
                "repo": blocked_repo,
                "branch_name": "feature/disallowed-repo",
                "from_branch": "main",
            },
        },
    )
    denied_id = denied_approval.json()["id"]
    await client.post(
        f"/api/v1/approvals/{denied_id}/decide",
        headers=auth_headers,
        json={"decision": "approved"},
    )
    denied = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={
            "approval_id": denied_id,
            "repo": blocked_repo,
            "branch_name": "feature/disallowed-repo",
            "from_branch": "main",
        },
    )
    assert denied.status_code == 403
    assert len(gh_calls) == 2


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
    assert revoked.json()["id"] == device_id
    assert revoked.json()["token_version"] == 2
    assert set(revoked.json()) == {"id", "token_version", "device_token"}
    assert DeviceTokenRevokeResponse.model_validate(revoked.json()).token_version == 2
    assert (await client.get(status_url, headers=old_headers)).status_code == 401

    new_token = revoked.json()["device_token"]
    assert new_token != old_token
    assert new_token == issue_device_token(device_id, 2)
    assert (await client.get(status_url, headers={"X-AATM-Device-Token": new_token})).status_code == 200

    assert any(
        event["type"] == "device.token_revoked"
        and event["payload"].get("device_id") == device_id
        and event["payload"].get("token_version") == 2
        for event in event_bus.recent_events()
    )
    async with AsyncSessionLocal() as db:
        audit = await db.execute(
            select(AuditLog).where(AuditLog.action == "device.token_revoked", AuditLog.resource_id == device_id)
        )
        assert audit.scalar_one_or_none() is not None

    revoked_again = await client.post(f"/api/v1/devices/{device_id}/revoke", headers=auth_headers)
    assert revoked_again.status_code == 200
    assert revoked_again.json()["token_version"] == 3
    latest_token = revoked_again.json()["device_token"]
    assert (await client.get(status_url, headers={"X-AATM-Device-Token": new_token})).status_code == 401
    assert (await client.get(status_url, headers={"X-AATM-Device-Token": latest_token})).status_code == 200


async def test_device_token_revoke_requires_admin_or_operator(client: AsyncClient, auth_headers):
    device_id = "token-revoke-role-v2-test"
    async with AsyncSessionLocal() as db:
        db.add(
            ExecutionDevice(
                id=device_id,
                name="Role Guard Device",
                device_type="remote_server",
                host_address="127.0.0.8",
                os_name="Linux",
            )
        )
        await db.commit()

    operator_headers = await _headers_for_role("operator")
    worker_headers = await _headers_for_role("worker")
    path = f"/api/v1/devices/{device_id}/revoke"
    assert (await client.post(path)).status_code == 401

    forbidden = await client.post(path, headers=worker_headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"].startswith("Requires role in")

    authorized = await client.post(path, headers=operator_headers)
    assert authorized.status_code == 200
    assert authorized.json()["token_version"] == 2
    assert set(authorized.json()) == {"id", "token_version", "device_token"}

    # The administrator role remains explicitly authorized by the same policy.
    admin_authorized = await client.post(path, headers=auth_headers)
    assert admin_authorized.status_code == 200
    assert admin_authorized.json()["token_version"] == 3


async def test_concurrent_device_token_revocations_are_atomic_and_monotonic(client: AsyncClient, auth_headers):
    device_id = "token-revoke-concurrent-v2-test"
    registered = await client.post(
        f"/api/v1/devices/{device_id}/register",
        headers=auth_headers,
        json={
            "name": "Concurrent Revocation Device",
            "device_type": "remote_server",
            "host_address": "127.0.0.9",
            "os_name": "Linux",
        },
    )
    assert registered.status_code == 200

    path = f"/api/v1/devices/{device_id}/revoke"
    responses = await asyncio.gather(*(client.post(path, headers=auth_headers) for _ in range(4)))
    assert [response.status_code for response in responses] == [200] * 4
    versions = sorted(response.json()["token_version"] for response in responses)
    assert versions == [2, 3, 4, 5]
    assert all(set(response.json()) == {"id", "token_version", "device_token"} for response in responses)

    status_url = f"/api/v1/devices/{device_id}/status"
    for response in responses:
        token = response.json()["device_token"]
        result = await client.get(status_url, headers={"X-AATM-Device-Token": token})
        if response.json()["token_version"] == max(versions):
            assert result.status_code == 200
        else:
            assert result.status_code == 401


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

    async with AsyncSessionLocal() as db:
        expected_budget = await provider_router.get_spend_summary(db)

    response = await client.get("/api/v1/providers/usage")
    assert response.status_code == 200
    assert "summary" in response.json() and "logs" in response.json()
    typed_usage = ProviderUsageResponse.model_validate(response.json())
    assert typed_usage.budget_summary.daily_limit_usd >= 0
    budget = response.json()["budget_summary"]
    assert budget == expected_budget
    assert budget["global_daily_spend_usd"] >= 2.5
    assert budget["global_monthly_spend_usd"] >= 2.5
    assert budget["daily_remaining_usd"] == pytest.approx(
        max(0.0, budget["daily_limit_usd"] - budget["global_daily_spend_usd"])
    )
    assert budget["monthly_remaining_usd"] == pytest.approx(
        max(0.0, budget["monthly_limit_usd"] - budget["global_monthly_spend_usd"])
    )

    assert set(budget) == {
        "global_daily_spend_usd",
        "global_monthly_spend_usd",
        "daily_limit_usd",
        "monthly_limit_usd",
        "daily_remaining_usd",
        "monthly_remaining_usd",
    }


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
    default_request = ProviderTestRequest()
    assert default_request.agent_id is None and default_request.session_id is None


async def test_traces_combine_real_sources_filter_and_omit_credentials(client: AsyncClient, auth_headers):
    project_id = "trace-project-v2-test"
    agent_id = "trace-agent-v2-test"
    task_id = "trace-task-v2-test"
    trace_marker = "trace-secret-value-do-not-return"

    async with AsyncSessionLocal() as db:
        db.add(
            Project(
                id=project_id,
                name="Trace Contract Project",
                slug="trace-contract-project",
                description="",
            )
        )
        db.add(
            Agent(
                id=agent_id,
                name="Trace Test Agent",
                role="worker",
                specialty="trace test",
            )
        )
        await db.flush()
        db.add(
            Task(
                id=task_id,
                project_id=project_id,
                assigned_agent_id=agent_id,
                title="Trace source task",
            )
        )
        db.add(
            AuditLog(
                actor=f"trace-auditor token={trace_marker}",
                action="trace.task_started",
                resource_type="task",
                resource_id=task_id,
                details={"agent_id": agent_id, "api_key": trace_marker},
            )
        )
        db.add(
            ProviderUsageLog(
                provider_id="arena-agent",
                agent_id=agent_id,
                task_id=task_id,
                model_name="trace-test-model",
                input_tokens=11,
                output_tokens=7,
                estimated_cost_usd=0.02,
                latency_ms=12.0,
                success=False,
                error_message=f"Authorization: Bearer {trace_marker}",
            )
        )
        db.add(
            WorkspaceArtifact(
                agent_id=agent_id,
                project_id=project_id,
                task_id=task_id,
                relative_path=f"reports/token={trace_marker}.txt",
                action="create",
                summary=f"api_key={trace_marker}",
                exit_code=0,
            )
        )
        db.add(
            AgentMessage(
                task_id=task_id,
                project_id=project_id,
                sender_id=agent_id,
                recipient_id="manager-0",
                instructions=f"Authorization: Bearer {trace_marker}",
                response_payload={"token": trace_marker},
            )
        )
        await db.commit()

    response = await client.get(
        "/api/v1/traces",
        headers=auth_headers,
        params={"agent_id": agent_id, "project_id": project_id},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 4
    assert {trace["source"] for trace in payload["traces"]} == {
        "audit_log",
        "provider_usage_log",
        "workspace_artifact",
        "agent_message",
    }
    assert all(trace["agent_id"] == agent_id for trace in payload["traces"])
    assert all(trace["project_id"] == project_id for trace in payload["traces"])
    audit_trace = next(trace for trace in payload["traces"] if trace["source"] == "audit_log")
    artifact_trace = next(trace for trace in payload["traces"] if trace["source"] == "workspace_artifact")
    assert "[REDACTED]" in audit_trace["actor"]
    assert "[REDACTED]" in artifact_trace["relative_path"]
    assert trace_marker not in json.dumps(payload)
    assert all("details" not in trace and "error_message" not in trace for trace in payload["traces"])

    missing = await client.get(
        "/api/v1/traces",
        headers=auth_headers,
        params={"agent_id": "missing-trace-agent", "project_id": project_id},
    )
    assert missing.status_code == 200
    assert missing.json()["traces"] == []
    assert missing.json()["count"] == 0
