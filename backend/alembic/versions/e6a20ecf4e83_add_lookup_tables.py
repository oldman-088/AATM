"""add lookup job / aggregated result / provider result tables

Revision ID: e6a20ecf4e83
Revises: e0f1a2b3c4d5
Create Date: 2026-11-19 17:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e6a20ecf4e83"
down_revision: str | None = "e0f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # Idempotent: dev databases rebuilt by the test-suite's create_all may already have these tables.
    existing = _existing_tables()

    if "lookup_jobs" not in existing:
        op.create_table(
            "lookup_jobs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("phone_raw", sa.String(length=64), nullable=False),
            sa.Column("phone_e164", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("created_by", sa.String(length=120), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_lookup_jobs_phone_e164", "lookup_jobs", ["phone_e164"], unique=False)
        op.create_index("ix_lookup_jobs_status", "lookup_jobs", ["status"], unique=False)
        op.create_index("ix_lookup_jobs_created_at", "lookup_jobs", ["created_at"], unique=False)

    if "lookup_results" not in existing:
        op.create_table(
            "lookup_results",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=True),
            sa.Column("country", sa.String(length=64), nullable=True),
            sa.Column("carrier", sa.String(length=120), nullable=True),
            sa.Column("line_type", sa.String(length=32), nullable=True),
            sa.Column("spam_score", sa.Float(), nullable=True),
            sa.Column("is_spam", sa.Boolean(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("attributes", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["lookup_jobs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("job_id", name="uq_lookup_results_job_id"),
        )
        op.create_index("ix_lookup_results_job_id", "lookup_results", ["job_id"], unique=False)

    if "provider_results" not in existing:
        op.create_table(
            "provider_results",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("latency_ms", sa.Float(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["lookup_jobs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("job_id", "provider", name="uq_provider_results_job_provider"),
        )
        op.create_index("ix_provider_results_job_id", "provider_results", ["job_id"], unique=False)
        op.create_index("ix_provider_results_provider", "provider_results", ["provider"], unique=False)


def downgrade() -> None:
    existing = _existing_tables()
    for table in ("provider_results", "lookup_results", "lookup_jobs"):
        if table in existing:
            op.drop_table(table)
