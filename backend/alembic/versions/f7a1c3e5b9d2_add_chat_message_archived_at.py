"""add chat message archived_at (E12: chat clear archives instead of deleting)

Revision ID: f7a1c3e5b9d2
Revises: e6a20ecf4e83
Create Date: 2026-11-19 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7a1c3e5b9d2"
down_revision: str | None = "e6a20ecf4e83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent: dev databases rebuilt by create_all may already have the column.
    if "archived_at" not in _columns("chat_messages"):
        op.add_column("chat_messages", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if "archived_at" in _columns("chat_messages"):
        with op.batch_alter_table("chat_messages") as batch:
            batch.drop_column("archived_at")
