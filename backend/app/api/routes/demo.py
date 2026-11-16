"""B3 — read-only demo feed for the Command Bridge.

Only reachable when `DEMO_MODE` is on (never in production). Every frame is flagged `demo: true`
and the endpoint performs no writes: it reads the seeded employee ids and returns scripted frames.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import Agent
from app.runtime.demo import demo_disabled_reason, demo_frames, demo_mode_active

router = APIRouter(tags=["demo"], dependencies=[Depends(get_current_user)])


@router.get("/demo/code-stream")
async def demo_code_stream(
    limit: int = Query(default=12, ge=1, le=12),
    db: AsyncSession = Depends(get_db),
):
    if not demo_mode_active():
        raise HTTPException(status_code=404, detail=demo_disabled_reason())
    agents = (
        await db.execute(select(Agent.id).where(Agent.status != "retired").order_by(Agent.id.asc()))
    ).scalars().all()
    worker_ids = [agent_id for agent_id in agents if agent_id != "manager-0"] or list(agents)
    return {
        "demo": True,
        "tick_seconds": 3,
        "agents": demo_frames(list(worker_ids), limit=limit),
        "note": "Scripted demo frames (DEMO_MODE). Not produced by an agent.",
    }
