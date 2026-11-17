"""R7 manager on Arena: manager sessions on arena_sessions + the manager_jobs queue

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-11-17 07:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e0f1a2b3c4d5"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns("arena_sessions")}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _columns()
    with op.batch_alter_table("arena_sessions") as batch:
        if "kind" not in existing:
            batch.add_column(sa.Column("kind", sa.String(length=16), nullable=False, server_default="worker"))
            batch.create_index("ix_arena_sessions_kind", ["kind"])
        if "project_id" not in existing:
            batch.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        if "exchange_count" not in existing:
            batch.add_column(sa.Column("exchange_count", sa.Integer(), nullable=False, server_default="0"))
    if "manager_jobs" not in _tables():
        op.create_table(
            "manager_jobs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
            sa.Column("project_id", sa.String(length=36), nullable=True),
            sa.Column("request_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("chat_message_id", sa.String(length=36), nullable=True),
            sa.Column("reply_message_id", sa.String(length=36), nullable=True),
            sa.Column("review_task_id", sa.String(length=36), nullable=True),
            sa.Column("session_id", sa.String(length=36), nullable=True),
            sa.Column("context", sa.JSON(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("json_retries", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("raw_reply", sa.Text(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_manager_jobs_kind", "manager_jobs", ["kind"])
        op.create_index("ix_manager_jobs_status", "manager_jobs", ["status"])
        op.create_index("ix_manager_jobs_project_id", "manager_jobs", ["project_id"])
        op.create_index("ix_manager_jobs_review_task_id", "manager_jobs", ["review_task_id"])


def downgrade() -> None:
    if "manager_jobs" in _tables():
        op.drop_table("manager_jobs")
    existing = _columns()
    with op.batch_alter_table("arena_sessions") as batch:
        if "exchange_count" in existing:
            batch.drop_column("exchange_count")
        if "project_id" in existing:
            batch.drop_column("project_id")
        if "kind" in existing:
            batch.drop_index("ix_arena_sessions_kind")
            batch.drop_column("kind")
