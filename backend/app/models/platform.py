import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class ProviderAccount(Base):
    __tablename__ = "provider_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str] = mapped_column(String(64), ForeignKey("ai_providers.id"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    account_identifier: Mapped[str] = mapped_column(String(160), nullable=False)
    allocation_mode: Mapped[str] = mapped_column(String(24), default="hybrid", nullable=False)  # one_per_employee|shared|hybrid
    max_employees: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)  # active|rate_limited|offline
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProviderSession(Base):
    __tablename__ = "provider_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(String(64), ForeignKey("provider_accounts.id"), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    browser_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[str] = mapped_column(String(120), default=lambda: f"conv-{uuid.uuid4().hex[:10]}", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)  # active|idle|degraded|error|closed
    health_score: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Userscript(Base):
    __tablename__ = "userscripts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(140), nullable=False, unique=True)
    namespace: Mapped[str] = mapped_column(String(120), default="aatm.tampermonkey", nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="1.0.0", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    match_patterns: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    allowed_roles: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    allowed_agent_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    audit_status: Mapped[str] = mapped_column(String(24), default="approved", nullable=False)
    execution_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel_type: Mapped[str] = mapped_column(String(24), default="direct", nullable=False)  # direct|group|task|broadcast
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    required_tools: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    workspace_info: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    expected_deliverables: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    permissions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    execution_status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(24), default="delivered", nullable=False)  # pending|delivered|acknowledged|failed
    response_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ExecutionDevice(Base):
    __tablename__ = "execution_devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    device_type: Mapped[str] = mapped_column(String(32), default="local", nullable=False)  # local|remote_server|edge_device
    host_address: Mapped[str] = mapped_column(String(160), default="127.0.0.1", nullable=False)
    os_name: Mapped[str] = mapped_column(String(64), default="Linux", nullable=False)
    cpu_cores: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    ram_gb: Mapped[float] = mapped_column(Float, default=4.0, nullable=False)
    capabilities: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="online", nullable=False)  # online|offline|disconnected|degraded
    token_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    max_concurrent_tasks: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    active_tasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WorkspaceArtifact(Base):
    __tablename__ = "workspace_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    relative_path: Mapped[str] = mapped_column(String(400), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # create|modify|test|build
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    exit_code: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
