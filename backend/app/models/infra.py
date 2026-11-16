import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class AIProvider(Base):
    __tablename__ = "ai_providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(48), nullable=False)  # arena_local_bridge|arena_agent|anthropic_claude|openai_compatible
    endpoint_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    default_model: Mapped[str] = mapped_column(String(120), nullable=False)
    available_models: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    health_status: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)  # healthy|degraded|offline|unknown
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_limit_usd: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProviderUsageLog(Base):
    __tablename__ = "provider_usage_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str] = mapped_column(String(64), ForeignKey("ai_providers.id"), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens_estimated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    output_tokens_estimated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    confirmed_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pricing_known: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class BrowserProfile(Base):
    __tablename__ = "browser_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_dir: Mapped[str] = mapped_column(String(500), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), default="background", nullable=False)  # visible|background
    status: Mapped[str] = mapped_column(String(24), default="ready", nullable=False)  # ready|running|locked|crashed|stopped
    lock_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_url: Mapped[str] = mapped_column(String(500), default="about:blank", nullable=False)
    page_title: Mapped[str] = mapped_column(String(240), default="New Tab", nullable=False)
    viewport_width: Mapped[int] = mapped_column(Integer, default=1280, nullable=False)
    viewport_height: Mapped[int] = mapped_column(Integer, default=800, nullable=False)
    crash_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    memory_mb: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    engine_mode: Mapped[str] = mapped_column(String(32), default="playwright", nullable=False)
    last_snapshot_svg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 6 Arena connector: only the SHA-256 of the per-profile userscript token is stored.
    connector_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connector_token_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connector_status: Mapped[str] = mapped_column(String(24), default="offline", server_default="offline", nullable=False)
    needs_human_reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BrowserSession(Base):
    __tablename__ = "browser_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str] = mapped_column(String(64), ForeignKey("browser_profiles.id"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(24), default="background", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
