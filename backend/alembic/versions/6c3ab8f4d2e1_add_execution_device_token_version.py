"""Add per-device token version for token revocation.

Revision ID: 6c3ab8f4d2e1
Revises: ee92483cefa3
Create Date: 2026-11-15
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "6c3ab8f4d2e1"
down_revision: Union[str, None] = "ee92483cefa3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_devices",
        sa.Column("token_version", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("execution_devices", "token_version")
