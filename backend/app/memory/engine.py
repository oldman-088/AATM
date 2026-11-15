import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MemoryEntry


class MemoryEngine:
    """4-layer permissioned memory (employee, team, project, organization + decision/task)."""

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[a-zA-Z0-9\u0600-\u06FF_]+", text.lower()))

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
        stmt = select(MemoryEntry)
        if scope:
            stmt = stmt.where(MemoryEntry.scope == scope)
        if project_id:
            stmt = stmt.where(
                (MemoryEntry.project_id == project_id)
                | (MemoryEntry.scope.in_(["global", "organization"]))
            )
        if agent_id:
            stmt = stmt.where(MemoryEntry.agent_id == agent_id)

        res = await db.execute(stmt.order_by(MemoryEntry.importance.desc(), MemoryEntry.updated_at.desc()))
        entries = list(res.scalars().all())
        q_tokens = self._tokenize(query)

        scored: list[tuple[float, MemoryEntry]] = []
        for entry in entries:
            # Permission check: private employee memory is restricted to owner or manager/admin
            if entry.visibility == "private" and requesting_agent_id and requesting_agent_id != "manager-0":
                if entry.agent_id and entry.agent_id != requesting_agent_id:
                    continue

            if not q_tokens:
                score = float(entry.importance)
            else:
                doc_tokens = self._tokenize(f"{entry.title} {entry.content} {' '.join(entry.tags or [])}")
                overlap = len(q_tokens & doc_tokens)
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
