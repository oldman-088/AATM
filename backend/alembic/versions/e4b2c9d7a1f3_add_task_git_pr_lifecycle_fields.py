"""add task git worktree and pr lifecycle fields

Revision ID: e4b2c9d7a1f3
Revises: d8f1a3b5c7e9
Create Date: 2026-11-16 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b2c9d7a1f3"
down_revision: str | None = "d8f1a3b5c7e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("branch_name", sa.String(length=160), nullable=True))
    op.add_column("tasks", sa.Column("worktree_path", sa.String(length=500), nullable=True))
    op.add_column("tasks", sa.Column("commit_sha", sa.String(length=64), nullable=True))
    op.add_column("tasks", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("pr_url", sa.String(length=500), nullable=True))
    op.add_column("tasks", sa.Column("pr_status", sa.String(length=32), nullable=True))

    op.add_column("git_workflow_records", sa.Column("task_id", sa.String(length=36), nullable=True))
    op.add_column("git_workflow_records", sa.Column("pr_url", sa.String(length=500), nullable=True))
    op.create_index("ix_git_workflow_records_task_id", "git_workflow_records", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_git_workflow_records_task_id", table_name="git_workflow_records")
    op.drop_column("git_workflow_records", "pr_url")
    op.drop_column("git_workflow_records", "task_id")

    op.drop_column("tasks", "pr_status")
    op.drop_column("tasks", "pr_url")
    op.drop_column("tasks", "pr_number")
    op.drop_column("tasks", "commit_sha")
    op.drop_column("tasks", "worktree_path")
    op.drop_column("tasks", "branch_name")
