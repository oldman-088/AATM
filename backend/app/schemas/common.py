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


class AgentUpdate(BaseModel):
    status: str | None = None
    provider_id: str | None = None
    model_name: str | None = None
    is_enabled: bool | None = None
    permissions: list[str] | None = None


class TaskCreate(BaseModel):
    project_id: str
    division_code: str = "D01"
    title: str = Field(min_length=2, max_length=240)
    description: str = ""
    priority: str = Field(default="medium", pattern="^(critical|high|medium|low)$")
    assigned_agent_id: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    estimated_minutes: int = 45


class TaskUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    assigned_agent_id: str | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    result_summary: str | None = None
    error_message: str | None = None


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


class MemoryCreate(BaseModel):
    scope: str = Field(pattern="^(global|project|agent|task|decision)$")
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
