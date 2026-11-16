"""Add atomic-claim metadata to agent messages.

Revision ID: f31b7e4a6d20
Revises: b7f8e17d4c20
Create Date: 2026-11-15
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "f31b7e4a6d20"
down_revision: Union[str, None] = "b7f8e17d4c20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("agent_messages")}
    if "claimed_by" not in columns:
        op.add_column("agent_messages", sa.Column("claimed_by", sa.String(length=64), nullable=True))
    if "claimed_at" not in columns:
        op.add_column("agent_messages", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))

    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("agent_messages")}
    if "ix_agent_messages_execution_claim" not in indexes:
        op.create_index(
            "ix_agent_messages_execution_claim",
            "agent_messages",
            ["execution_status", "claimed_by", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("agent_messages")}
    if "ix_agent_messages_execution_claim" in indexes:
        op.drop_index("ix_agent_messages_execution_claim", table_name="agent_messages")

    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_messages")}
    if "claimed_at" in columns:
        op.drop_column("agent_messages", "claimed_at")
    if "claimed_by" in columns:
        op.drop_column("agent_messages", "claimed_by")
