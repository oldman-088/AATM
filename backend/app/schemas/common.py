import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_CREDENTIAL_REF_PATTERN = re.compile(r"env:[A-Z_][A-Z0-9_]*")
CREDENTIAL_REF_ERROR = "credential_ref must reference an environment variable (env:NAME); inline credentials are not accepted."


def validate_credential_ref(value: str | None) -> str | None:
    """Validate an environment-variable credential reference without echoing its value on failure."""
    if value is not None and _CREDENTIAL_REF_PATTERN.fullmatch(value) is None:
        raise ValueError(CREDENTIAL_REF_ERROR)
    return value


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]


class UserPreferencesUpdate(BaseModel):
    language: str | None = Field(default=None, pattern="^(en|ar|fa)$")
    theme: str | None = Field(default=None, pattern="^(light|dark|system)$")


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    description: str = ""
    repo_path: str = "."
    repo_url: str | None = None
    default_branch: str = "main"
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master", "production"])
    tech_stack: list[str] = Field(default_factory=list)
    worktree_mode: bool | None = None
    auto_pr: bool = True


class ProjectUpdate(BaseModel):
    description: str | None = None
    repo_url: str | None = None
    default_branch: str | None = None
    # Branch work starts from and pull requests target, when it is not the default branch.
    base_branch: str | None = Field(default=None, min_length=1, max_length=80)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    # The project the dashboard and Manager Chat open by default.
    active: bool | None = None
    worktree_mode: bool | None = None
    auto_pr: bool | None = None
    # Worker ids the manager may plan for ([] = every enabled worker).
    team: list[str] | None = Field(default=None, max_length=50)
    test_command: str | None = Field(default=None, max_length=500)


class EmployeeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    role_template_id: str | None = None
    reports_to: str | None = Field(default=None, max_length=64)
    specialty: str | None = None
    skills: list[str] | None = None
    divisions: list[str] | None = None
    permissions: list[str] | None = None
    provider_id: str = "arena-agent"
    model_name: str | None = Field(default=None, max_length=120)
    allocation_mode: str = Field(default="hybrid", pattern="^(one_per_employee|shared|hybrid)$")
    preferred_account_id: str | None = None
    create_browser_profile: bool = True
    autonomy_mode: str = Field(default="semi_autonomous", pattern="^(supervised|semi_autonomous|highly_autonomous)$")
    device_id: str = "device-local"


class EmployeeSkillGapRequest(BaseModel):
    required_skills: list[str]
    divisions: list[str] | None = None


class AgentUpdate(BaseModel):
    status: str | None = None
    provider_id: str | None = None
    model_name: str | None = None
    reports_to: str | None = Field(default=None, max_length=64)
    is_enabled: bool | None = None
    permissions: list[str] | None = None
    autonomy_mode: str | None = None


class TaskCreate(BaseModel):
    project_id: str
    division_code: str = "D01"
    title: str = Field(min_length=2, max_length=240)
    description: str = ""
    priority: str = Field(default="medium", pattern="^(critical|high|medium|low)$")
    execution_mode: str = Field(default="automatic", pattern="^(manual|automatic|collaborative)$")
    assigned_agent_id: str | None = None
    collaborator_ids: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    expected_deliverables: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    estimated_minutes: int = 45


class TaskUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    execution_mode: str | None = None
    assigned_agent_id: str | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    result_summary: str | None = None
    error_message: str | None = None
    review_status: str | None = None


class ProviderCreateOrUpdate(BaseModel):
    id: str | None = None
    name: str
    provider_type: str = Field(pattern="^(arena_local_bridge|arena_agent|anthropic_claude|openai_compatible)$")
    endpoint_url: str | None = None
    credential_ref: str | None = Field(
        default=None,
        description="Environment-variable reference in env:NAME form; inline credentials are not accepted.",
    )
    default_model: str
    available_models: list[str] = Field(default_factory=list)
    priority: int = 10
    is_enabled: bool = True
    daily_limit_usd: float = 20.0


class ProviderAccountCreate(BaseModel):
    provider_id: str = "arena-agent"
    label: str
    account_identifier: str
    allocation_mode: str = Field(default="hybrid", pattern="^(one_per_employee|shared|hybrid)$")
    max_employees: int = Field(default=5, ge=1, le=50)
    credential_ref: str | None = Field(
        default=None,
        description=(
            "Optional provider API-key reference (env:<NAME>_API_KEY). Arena accounts use the human's own "
            "browser sign-in and carry no credential."
        ),
    )


class ProviderTestRequest(BaseModel):
    prompt: str = "Respond with AATM connectivity verification."
    model: str | None = None
    dry_run: bool = True
    agent_id: str | None = None
    session_id: str | None = None


class ProviderUsageBudgetSummary(BaseModel):
    global_daily_spend_usd: float
    global_monthly_spend_usd: float
    daily_limit_usd: float
    monthly_limit_usd: float
    daily_remaining_usd: float
    monthly_remaining_usd: float


class ProviderUsageTotals(BaseModel):
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost_usd: float
    pricing_known: bool = True
    has_unknown_pricing: bool = False


class ProviderUsageLogEntry(BaseModel):
    id: str
    provider_id: str
    agent_id: str | None
    model_name: str
    input_tokens: int
    output_tokens: int
    input_tokens_estimated: bool
    output_tokens_estimated: bool
    estimated_cost_usd: float
    confirmed_cost_usd: float | None
    pricing_known: bool = False
    latency_ms: float
    success: bool
    error_message: str | None
    created_at: datetime


class ProviderWizardTestRequest(BaseModel):
    provider_type: str = Field(pattern="^(anthropic_claude|openai_compatible|arena_local_bridge|arena_agent)$")
    endpoint_url: str | None = None
    credential_ref: str = Field(
        description="Environment-variable reference in env:NAME form; inline credentials are not accepted.",
    )
    model: str = Field(min_length=1, max_length=120)


class ProviderWizardConfigureRequest(BaseModel):
    id: str | None = None
    name: str | None = None
    provider_type: str = Field(pattern="^(anthropic_claude|openai_compatible|arena_local_bridge|arena_agent)$")
    endpoint_url: str | None = None
    credential_ref: str = Field(
        description="Environment-variable reference in env:NAME form; inline credentials are not accepted.",
    )
    default_model: str = Field(min_length=1, max_length=120)
    available_models: list[str] = Field(default_factory=list)
    daily_limit_usd: float = 20.0
    assign_to_manager: bool = False
    assign_to_workers: bool = False


class ProviderUsageResponse(BaseModel):
    budget_summary: ProviderUsageBudgetSummary
    summary: ProviderUsageTotals
    logs: list[ProviderUsageLogEntry]


class DeviceTokenRevokeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    token_version: int = Field(ge=1)
    device_token: str = Field(pattern=r"^[a-f0-9]{64}$")


class RemoteWorkspaceTreeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=400)
    size_bytes: int = Field(ge=0)


class RemoteWorkspaceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=400)
    content: str = Field(max_length=3_000)


class RemoteActionIteration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    iteration: int = Field(ge=1, le=100)
    action: str = Field(min_length=1, max_length=32)
    result: str = Field(default="", max_length=4_000)
    correction: str | None = Field(default=None, max_length=1_000)


class DeviceAgentModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    worker_id: str = Field(min_length=1, max_length=64)
    workspace_file_tree: list[RemoteWorkspaceTreeEntry] = Field(default_factory=list, max_length=200)
    relevant_workspace_file_contents: list[RemoteWorkspaceFile] = Field(default_factory=list, max_length=5)
    previous_iterations: list[RemoteActionIteration] = Field(default_factory=list, max_length=8)
    last_correction: str | None = Field(default=None, max_length=1_000)


class RemoteArtifactReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    relative_path: str = Field(min_length=1, max_length=400)
    action: str = Field(pattern="^(read|create|modify|test|build|agent_iteration)$")
    exit_code: int = Field(ge=-127, le=32_767)
    summary: str = Field(default="", max_length=1_000)


class DeviceTaskProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    worker_id: str = Field(min_length=1, max_length=64)
    progress_percent: int = Field(ge=0, le=95)
    event: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    summary: str = Field(default="", max_length=1_000)
    iteration: int = Field(default=0, ge=0, le=100)
    artifact: RemoteArtifactReport | None = None


class DeviceTaskReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    worker_id: str = Field(min_length=1, max_length=64)
    success: bool
    summary: str = Field(default="", max_length=4_000)
    artifacts: list[RemoteArtifactReport] = Field(default_factory=list, max_length=100)
    actual_deliverables: list[str] = Field(default_factory=list, max_length=100)
    error_message: str | None = Field(default=None, max_length=2_000)
    trace: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    total_elapsed_ms: float | None = Field(default=None, ge=0)
    keepalive_heartbeats: int = Field(default=0, ge=0)


class BrowserLaunchRequest(BaseModel):
    mode: str = Field(default="background", pattern="^(visible|background)$")
    url: str | None = None
    owner: str = "manager-0"


class BrowserCreateRequest(BaseModel):
    id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=120)
    agent_id: str | None = None
    mode: str = Field(default="background", pattern="^(visible|background)$")
    current_url: str = "about:blank"


class BrowserNavigateRequest(BaseModel):
    url: str
    title: str | None = None


class BrowserWorkflowRequest(BaseModel):
    engine: str = "automa"
    steps: list[dict[str, Any]]
    agent_id: str | None = None


class BrowserExtractTextRequest(BaseModel):
    selector: str = Field(min_length=1)
    max_chars: int = 2000


class UserscriptCreate(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    description: str = ""
    match_patterns: list[str]
    allowed_roles: list[str] = Field(default_factory=lambda: ["worker", "manager"])
    allowed_agent_ids: list[str] = Field(default_factory=list)
    code: str = Field(min_length=5)


class AgentMessageSend(BaseModel):
    task_id: str | None = None
    project_id: str | None = None
    sender_id: str = "manager-0"
    recipient_id: str
    instructions: str = Field(min_length=2)
    priority: str = "high"
    expected_deliverables: list[str] = Field(default_factory=lambda: ["deliverable_report.md"])
    auto_execute: bool = True


class WorkspaceWriteRequest(BaseModel):
    relative_path: str = Field(min_length=1, max_length=300)
    content: str
    project_id: str | None = None
    task_id: str | None = None


class WorkspaceExecRequest(BaseModel):
    command: list[str]
    project_id: str | None = None
    task_id: str | None = None


class SDLCRequest(BaseModel):
    project_name: str = Field(min_length=2, max_length=120)
    idea: str = Field(min_length=5)


class DeviceRegisterRequest(BaseModel):
    id: str = Field(min_length=2, max_length=64)
    name: str
    device_type: str = Field(default="remote_server", pattern="^(local|remote_server|edge_device)$")
    host_address: str
    os_name: str = "Ubuntu 24.04"
    cpu_cores: int = 4
    ram_gb: float = 8.0
    capabilities: list[str] = Field(default_factory=lambda: ["python", "playwright"])
    max_concurrent_tasks: int = 4


class EmergencyStopRequest(BaseModel):
    scope: str = Field(default="global", pattern="^(global|employee|task|browser)$")
    target_id: str | None = None
    reason: str = "Operator triggered emergency stop"


class SanitizeRequest(BaseModel):
    text: str
    source: str = "external"


class MemoryCreate(BaseModel):
    scope: str = Field(pattern="^(employee|team|project|organization|global|agent|task|decision)$")
    visibility: str = Field(default="project", pattern="^(private|team|project|organization)$")
    project_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    title: str = Field(min_length=2, max_length=200)
    content: str = Field(min_length=2)
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)


class ChatSendRequest(BaseModel):
    project_id: str | None = None
    content: str = Field(min_length=1)
    recipient_id: str | None = "manager-0"
    # R7 (manager on Arena): chat | goal | status; empty = detected from the message (see manager_arena).
    intent: str | None = Field(default=None, pattern="^(auto|chat|goal|status)$")


class ApprovalCreate(BaseModel):
    project_id: str | None = None
    action_type: str = Field(pattern="^(branch|spend|deploy|merge|delete|credential|security|irreversible)$")
    title: str
    description: str
    estimated_cost_usd: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    review_note: str | None = None


class _GitHubWriteApprovalRequest(BaseModel):
    approval_id: str = Field(
        min_length=1,
        description="ID of an approved governance request; integrations must verify approval before writing.",
    )
    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$", description="GitHub repository in owner/name form.")


class GitHubBranchCreateRequest(_GitHubWriteApprovalRequest):
    branch_name: str = Field(min_length=1, max_length=255)
    from_branch: str = Field(min_length=1, max_length=255)


class GitHubPRCreateRequest(_GitHubWriteApprovalRequest):
    title: str = Field(min_length=1, max_length=256)
    body: str = ""
    head: str = Field(min_length=1)
    base: str = Field(default="main", min_length=1)
    draft: bool = False


class GitHubIssueCreateRequest(_GitHubWriteApprovalRequest):
    title: str = Field(min_length=1, max_length=256)
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)


class GitHubPRCommentRequest(_GitHubWriteApprovalRequest):
    approval_id: str | None = Field(default=None, min_length=1)
    body: str = Field(min_length=1, max_length=65_536)
    event: str = Field(default="COMMENT", pattern="^(COMMENT|APPROVE|REQUEST_CHANGES)$")


class GitWorktreeCreateRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    branch_name: str = Field(min_length=1, max_length=200)
    base_ref: str = Field(default="HEAD", min_length=1, max_length=200)
    approval_id: str | None = Field(default=None, min_length=1)


class GitWorkflowCreate(BaseModel):
    project_id: str
    branch_name: str
    base_branch: str = "main"
    owner_agent_id: str | None = None
    worktree_path: str | None = None


class SettingUpdate(BaseModel):
    value: dict[str, Any]
    description: str | None = None


class StatusSummary(BaseModel):
    timestamp: datetime
    status: str
