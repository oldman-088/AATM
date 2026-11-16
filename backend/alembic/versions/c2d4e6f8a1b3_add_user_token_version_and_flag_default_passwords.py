"""Add User.token_version and flag existing default/placeholder passwords.

Revision ID: c2d4e6f8a1b3
Revises: 9a4e3d2c1b0f
Create Date: 2026-11-16 07:45:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import context, op
from app.core.config import KNOWN_DEFAULT_PASSWORDS
from app.core.security import verify_password

revision: str = "c2d4e6f8a1b3"
down_revision: Union[str, None] = "9a4e3d2c1b0f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    if context.is_offline_mode():
        return

    bind = op.get_bind()
    users_table = sa.table(
        "users",
        sa.column("id", sa.String()),
        sa.column("password_hash", sa.String()),
        sa.column("must_change_password", sa.Boolean()),
    )
    rows = bind.execute(
        sa.select(users_table.c.id, users_table.c.password_hash).where(
            users_table.c.must_change_password.is_(False)
        )
    ).fetchall()
    flagged_ids = [
        row.id
        for row in rows
        if any(verify_password(candidate, row.password_hash) for candidate in KNOWN_DEFAULT_PASSWORDS)
    ]
    if flagged_ids:
        bind.execute(
            users_table.update()
            .where(users_table.c.id.in_(flagged_ids))
            .values(must_change_password=True)
        )


def downgrade() -> None:
    op.drop_column("users", "token_version")
