"""Add the nullable agent supervisor reference.

Revision ID: 629b77662bf2
Revises: 6c3ab8f4d2e1
Create Date: 2026-11-15 12:21:44.698516
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "629b77662bf2"
down_revision: Union[str, None] = "6c3ab8f4d2e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("reports_to", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "reports_to")
