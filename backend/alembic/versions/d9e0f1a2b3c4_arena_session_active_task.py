"""Arena Agent: arena_sessions.active_task_id (correction follow-ups reuse the session)

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-11-16 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns("arena_sessions")}


def upgrade() -> None:
    if "active_task_id" not in _columns():
        with op.batch_alter_table("arena_sessions") as batch:
            batch.add_column(sa.Column("active_task_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    if "active_task_id" in _columns():
        with op.batch_alter_table("arena_sessions") as batch:
            batch.drop_column("active_task_id")
