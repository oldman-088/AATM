"""Add flags that distinguish reported token usage from estimates.

Revision ID: b7f8e17d4c20
Revises: 629b77662bf2
Create Date: 2026-11-15
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b7f8e17d4c20"
down_revision: Union[str, None] = "629b77662bf2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("provider_usage_logs")}
    if "input_tokens_estimated" not in columns:
        op.add_column(
            "provider_usage_logs",
            sa.Column("input_tokens_estimated", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    if "output_tokens_estimated" not in columns:
        op.add_column(
            "provider_usage_logs",
            sa.Column("output_tokens_estimated", sa.Boolean(), server_default=sa.false(), nullable=False),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("provider_usage_logs")}
    if "output_tokens_estimated" in columns:
        op.drop_column("provider_usage_logs", "output_tokens_estimated")
    if "input_tokens_estimated" in columns:
        op.drop_column("provider_usage_logs", "input_tokens_estimated")
