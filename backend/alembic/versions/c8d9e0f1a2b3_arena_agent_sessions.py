"""Arena Agent mode: browser_profiles.arena_mode and the arena_sessions table

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-11-16 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    # Idempotent: dev databases rebuilt by the test-suite's create_all may already have these.
    inspector = _inspector()
    columns = {col["name"] for col in inspector.get_columns("browser_profiles")}
    if "arena_mode" not in columns:
        with op.batch_alter_table("browser_profiles") as batch:
            batch.add_column(sa.Column("arena_mode", sa.String(length=16), nullable=False, server_default="chat"))
    if "arena_sessions" not in inspector.get_table_names():
        op.create_table(
            "arena_sessions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("profile_id", sa.String(length=64), sa.ForeignKey("browser_profiles.id"), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=True),
            sa.Column("page_state", sa.String(length=32), nullable=False, server_default="offline"),
            sa.Column("phase", sa.String(length=32), nullable=False, server_default="new"),
            sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("keep_working_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("follow_up_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("correction_rounds", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("branch_name", sa.String(length=160), nullable=True),
            sa.Column("pr_number", sa.Integer(), nullable=True),
            sa.Column("pr_url", sa.String(length=500), nullable=True),
            sa.Column("head_sha", sa.String(length=64), nullable=True),
            sa.Column("report", sa.Text(), nullable=True),
            sa.Column("needs_human_reason", sa.String(length=240), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("events", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("last_state_change_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_arena_sessions_agent_id", "arena_sessions", ["agent_id"])
        op.create_index("ix_arena_sessions_profile_id", "arena_sessions", ["profile_id"])
        op.create_index("ix_arena_sessions_task_id", "arena_sessions", ["task_id"])
        op.create_index("ix_arena_sessions_phase", "arena_sessions", ["phase"])
        op.create_index("ix_arena_sessions_is_open", "arena_sessions", ["is_open"])


def downgrade() -> None:
    inspector = _inspector()
    if "arena_sessions" in inspector.get_table_names():
        op.drop_table("arena_sessions")
    columns = {col["name"] for col in inspector.get_columns("browser_profiles")}
    if "arena_mode" in columns:
        with op.batch_alter_table("browser_profiles") as batch:
            batch.drop_column("arena_mode")
