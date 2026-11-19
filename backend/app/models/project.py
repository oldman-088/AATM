import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(140), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    repo_path: Mapped[str] = mapped_column(String(500), default=".", nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    default_branch: Mapped[str] = mapped_column(String(80), default="main", nullable=False)
    protected_branches: Mapped[list] = mapped_column(JSON, default=lambda: ["main", "master", "production"], nullable=False)
    tech_stack: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)  # active | completed | archived
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


def configured_base_branch(project: Project | None) -> str | None:
    """The branch explicitly set for this project's work (``config.base_branch``), if any."""
    config = project.config if project is not None and isinstance(project.config, dict) else {}
    return str(config.get("base_branch") or "").strip() or None


def project_base_branch(project: Project | None) -> str:
    """The branch work starts from and pull requests target."""
    return configured_base_branch(project) or (project.default_branch if project is not None else "") or "main"


class MemoryEntry(Base):
    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Scopes: employee | team | project | organization | global | agent | task | decision
    scope: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(24), default="project", nullable=False)  # private|team|project|organization
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), default="manager", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class GitWorkflowRecord(Base):
    __tablename__ = "git_workflow_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    branch_name: Mapped[str] = mapped_column(String(160), nullable=False)
    worktree_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    owner_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_branch: Mapped[str] = mapped_column(String(80), default="main", nullable=False)
    last_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pr_status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    checks_passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_protected_target: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
