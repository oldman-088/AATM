"""Phase 8: Postgres full-text search index for memory entries.

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1

A GIN expression index over the same ``to_tsvector('simple', ...)`` expression the memory
engine queries (``simple`` keeps Arabic/Persian words intact; no language stemming). No-op on
SQLite, which keeps the bounded LIKE prefilter + token scoring fallback.
"""

from __future__ import annotations

from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None

INDEX = "ix_memory_entries_fts"
# Same expression as app.memory.engine.FTS_DOCUMENT_SQL (without the table prefix).
EXPRESSION = (
    "to_tsvector('simple'::regconfig, coalesce(title, '') || ' ' || "
    "coalesce(content, '') || ' ' || coalesce(CAST(tags AS TEXT), ''))"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"CREATE INDEX IF NOT EXISTS {INDEX} ON memory_entries USING gin ({EXPRESSION})")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
