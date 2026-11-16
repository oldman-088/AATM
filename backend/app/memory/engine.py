"""Permissioned memory search.

* PostgreSQL: full-text search (``to_tsvector('simple', …) @@ to_tsquery``) backed by the GIN
  expression index from migration ``b7c8d9e0f1a2``, ranked with ``ts_rank`` + importance.
* SQLite (single-machine fallback): bounded LIKE prefilter + token-overlap scoring.

Embeddings (pgvector) are not implemented: ``search_backend()`` reports what is active.
Scope/visibility rules are identical on both backends.
"""

import re
from typing import Any

from sqlalchemy import String, cast, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MemoryEntry

MAX_QUERY_TOKENS = 8


# Must stay textually identical (modulo the table prefix) to the index expression in migration
# b7c8d9e0f1a2, otherwise Postgres cannot use the GIN index. Constants are inlined on purpose:
# bound parameters would not match an expression index.
FTS_DOCUMENT_SQL = (
    "to_tsvector('simple'::regconfig, coalesce(memory_entries.title, '') || ' ' || "
    "coalesce(memory_entries.content, '') || ' ' || coalesce(CAST(memory_entries.tags AS TEXT), ''))"
)
SIMPLE_CONFIG = literal_column("'simple'::regconfig")


def _fts_document():
    return literal_column(FTS_DOCUMENT_SQL)


class MemoryEngine:
    """4-layer permissioned memory (employee, team, project, organization + decision/task)."""

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[a-zA-Z0-9؀-ۿ_]+", text.lower()))

    @staticmethod
    def search_backend(db: AsyncSession) -> dict[str, Any]:
        dialect = db.bind.dialect.name if db.bind is not None else "unknown"
        return {
            "backend": "postgres_fts" if dialect == "postgresql" else "token_overlap",
            "dialect": dialect,
            "embeddings": "not_implemented",
        }

    @staticmethod
    def _visible(entry: MemoryEntry, requesting_agent_id: str | None) -> bool:
        # Private employee memory is restricted to its owner or the manager/admin.
        if entry.visibility == "private" and requesting_agent_id and requesting_agent_id != "manager-0":
            if entry.agent_id and entry.agent_id != requesting_agent_id:
                return False
        return True

    async def search(
        self,
        db: AsyncSession,
        query: str = "",
        scope: str | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
        requesting_agent_id: str | None = None,
        limit: int = 30,
    ) -> list[dict]:
        q_tokens = sorted(self._tokenize(query))[:MAX_QUERY_TOKENS]
        stmt = select(MemoryEntry)
        if scope:
            stmt = stmt.where(MemoryEntry.scope == scope)
        if project_id:
            stmt = stmt.where((MemoryEntry.project_id == project_id) | (MemoryEntry.scope.in_(["global", "organization"])))
        if agent_id:
            stmt = stmt.where(MemoryEntry.agent_id == agent_id)
        limit = max(int(limit or 30), 1)
        candidate_limit = limit * 5

        if q_tokens and self.search_backend(db)["backend"] == "postgres_fts":
            # Tokens are [a-z0-9_ + Arabic] only, so they cannot inject tsquery operators.
            tsquery = func.to_tsquery(SIMPLE_CONFIG, " | ".join(f"{tok}:*" for tok in q_tokens))
            document = _fts_document()
            rank = func.ts_rank(document, tsquery).label("rank")
            res = await db.execute(
                stmt.add_columns(rank)
                .where(document.op("@@")(tsquery))
                .order_by(rank.desc(), MemoryEntry.importance.desc(), MemoryEntry.updated_at.desc())
                .limit(candidate_limit)
            )
            scored = [
                (float(r) * 10.0 + entry.importance * 0.5, entry)
                for entry, r in res.all()
                if self._visible(entry, requesting_agent_id)
            ]
        else:
            if q_tokens:
                stmt = stmt.where(
                    or_(
                        *[
                            or_(
                                MemoryEntry.title.ilike(f"%{tok}%"),
                                MemoryEntry.content.ilike(f"%{tok}%"),
                                cast(MemoryEntry.tags, String).ilike(f"%{tok}%"),
                            )
                            for tok in q_tokens
                        ]
                    )
                )
            res = await db.execute(stmt.order_by(MemoryEntry.importance.desc(), MemoryEntry.updated_at.desc()).limit(candidate_limit))
            token_set = set(q_tokens)
            scored = []
            for entry in res.scalars().all():
                if not self._visible(entry, requesting_agent_id):
                    continue
                if not token_set:
                    score = float(entry.importance)
                else:
                    overlap = len(token_set & self._tokenize(f"{entry.title} {entry.content} {' '.join(entry.tags or [])}"))
                    if overlap == 0:
                        continue
                    score = overlap * 3.0 + (entry.importance * 0.5)
                scored.append((score, entry))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "id": e.id,
                "scope": e.scope,
                "visibility": e.visibility,
                "project_id": e.project_id,
                "agent_id": e.agent_id,
                "task_id": e.task_id,
                "title": e.title,
                "content": e.content,
                "tags": e.tags,
                "importance": e.importance,
                "created_by": e.created_by,
                "relevance_score": round(s, 2),
                "updated_at": e.updated_at,
            }
            for s, e in scored[:limit]
        ]


memory_engine = MemoryEngine()
