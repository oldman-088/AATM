"""B1 — live code stream API for the Command Bridge.

The Bridge subscribes to the authenticated ``/ws/events`` socket for new ``agent.code`` events and
uses these read-only endpoints for the initial paint (and after a reconnect). Nothing here writes
state: the frames are produced by the real agent runtime (workspace writes, pytest/ruff runs,
pull-request review comments) and kept in a bounded in-memory buffer per agent.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import Agent
from app.runtime.code_stream import MAX_BUFFER_FRAMES, code_stream

router = APIRouter(tags=["code-stream"], dependencies=[Depends(get_current_user)])


@router.get("/agents/{agent_id}/code-stream")
async def agent_code_stream(
    agent_id: str,
    limit: int = Query(default=40, ge=1, le=MAX_BUFFER_FRAMES),
    db: AsyncSession = Depends(get_db),
):
    """Recent redacted code frames for one employee (oldest first)."""
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    frames = code_stream.buffer(agent_id, limit)
    return {
        "agent_id": agent_id,
        "agent_name": agent.name,
        "count": len(frames),
        "limit": limit,
        "frames": frames,
    }


@router.get("/code-stream")
async def bridge_code_stream(
    limit: int = Query(default=12, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
):
    """Initial paint for the whole bridge: the last ``limit`` frames of every enabled employee."""
    agents = list((await db.execute(select(Agent).where(Agent.is_enabled.is_(True)))).scalars())
    return {
        "agents": {agent.id: code_stream.buffer(agent.id, limit) for agent in agents},
        "pending": code_stream.pending_count(),
    }
