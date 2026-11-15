from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    description: str = ""
    repo_path: str = "."
    repo_url: str | None = None
    default_branch: str = "main"
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master", "production"])
    tech_stack: list[str] = Field(default_factory=list)


class EmployeeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    role_template_id: str | None = None
    specialty: str | None = None
    skills: list[str] | None = None
    divisions: list[str] | None = None
    permissions: list[str] | None = None
    provider_id: str = "arena-agent"
    allocation_mode: str = Field(default="hybrid", pattern="^(one_per_employee|shared|hybrid)$")
    preferred_account_id: str | None = None
    create_browser_profile: bool = True
    autonomy_mode: str = Field(default="semi_autonomous", pattern="^(supervised|semi_autonomous|highly_autonomous)$")
    device_id: str = "device-local"


class AgentUpdate(BaseModel):
    status: str | None = None
    provider_id: str | None = None
    model_name: str | None = None
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
    provider_type: str = Field(pattern="^(arena_local_bridge|arena_agent|anthropic_claude)$")
    endpoint_url: str | None = None
    credential_ref: str | None = None
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
    credential_ref: str | None = "env:ARENA_SESSION_TOKEN"


class ProviderTestRequest(BaseModel):
    prompt: str = "Respond with AATM connectivity verification."
    model: str | None = None
    dry_run: bool = True


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


class ApprovalCreate(BaseModel):
    project_id: str | None = None
    action_type: str = Field(pattern="^(spend|deploy|merge|delete|credential|security|irreversible)$")
    title: str
    description: str
    estimated_cost_usd: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    review_note: str | None = None


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
