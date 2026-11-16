"""Add pricing_known flag to provider_usage_logs.

Revision ID: d8f1a3b5c7e9
Revises: c2d4e6f8a1b3
Create Date: 2026-11-16 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8f1a3b5c7e9"
down_revision: str | Sequence[str] | None = "c2d4e6f8a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_usage_logs",
        sa.Column("pricing_known", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("provider_usage_logs", "pricing_known")
