"""add review / merge / project completion fields (Phase 4)

Revision ID: f5a6b7c8d9e0
Revises: e4b2c9d7a1f3
Create Date: 2026-11-16 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5a6b7c8d9e0"
down_revision: str | None = "e4b2c9d7a1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    # Idempotent: dev databases rebuilt by the test-suite's create_all may already have these columns.
    task_cols = _columns("tasks")
    with op.batch_alter_table("tasks") as batch:
        if "task_type" not in task_cols:
            batch.add_column(sa.Column("task_type", sa.String(length=24), nullable=False, server_default="implementation"))
        if "parent_task_id" not in task_cols:
            batch.add_column(sa.Column("parent_task_id", sa.String(length=36), nullable=True))
        if "review_round" not in task_cols:
            batch.add_column(sa.Column("review_round", sa.Integer(), nullable=False, server_default="0"))
    task_indexes = _indexes("tasks")
    if "ix_tasks_parent_task_id" not in task_indexes:
        op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"], unique=False)
    if "ix_tasks_task_type" not in task_indexes:
        op.create_index("ix_tasks_task_type", "tasks", ["task_type"], unique=False)
    project_cols = _columns("projects")
    with op.batch_alter_table("projects") as batch:
        if "completed_at" not in project_cols:
            batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        if "completion_summary" not in project_cols:
            batch.add_column(sa.Column("completion_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("completion_summary")
        batch.drop_column("completed_at")
    op.drop_index("ix_tasks_task_type", table_name="tasks")
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("review_round")
        batch.drop_column("parent_task_id")
        batch.drop_column("task_type")
