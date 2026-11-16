"""Add User.must_change_password flag for first-login password rotation.

Revision ID: 9a4e3d2c1b0f
Revises: 629b77662bf2
Create Date: 2026-11-15 14:05:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "9a4e3d2c1b0f"
down_revision: Union[str, None] = "f31b7e4a6d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
