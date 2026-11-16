"""add Arena browser connector fields to browser_profiles (Phase 6)

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-11-16 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a6b7c8d9e0f1"
down_revision: str | None = "f5a6b7c8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_COLUMNS = (
    ("connector_token_hash", lambda: sa.Column("connector_token_hash", sa.String(length=64), nullable=True)),
    ("connector_token_rotated_at", lambda: sa.Column("connector_token_rotated_at", sa.DateTime(timezone=True), nullable=True)),
    ("connector_status", lambda: sa.Column("connector_status", sa.String(length=24), nullable=False, server_default="offline")),
    ("needs_human_reason", lambda: sa.Column("needs_human_reason", sa.String(length=240), nullable=True)),
)


def _columns(table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent: dev databases rebuilt by the test-suite's create_all may already have these columns.
    existing = _columns("browser_profiles")
    with op.batch_alter_table("browser_profiles") as batch:
        for name, make in NEW_COLUMNS:
            if name not in existing:
                batch.add_column(make())


def downgrade() -> None:
    existing = _columns("browser_profiles")
    with op.batch_alter_table("browser_profiles") as batch:
        for name, _ in reversed(NEW_COLUMNS):
            if name in existing:
                batch.drop_column(name)
