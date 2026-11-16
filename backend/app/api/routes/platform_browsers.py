"""Browser engines and userscripts (stored and reviewed; never injected by AATM)."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.browser.engines import BROWSER_ENGINE_REGISTRY, validate_userscript_policy
from app.db.session import get_db
from app.models import (
    User,
    Userscript,
)
from app.schemas.common import (
    UserscriptCreate,
)

router = APIRouter(tags=["enterprise-platform"], dependencies=[Depends(get_current_user)])


@router.get("/browsers/engines")
async def list_browser_engines():
    return {k: v.capabilities() for k, v in BROWSER_ENGINE_REGISTRY.items()}


USERSCRIPT_STORAGE_ONLY_NOTICE = (
    "AATM stores and audits userscripts but does not inject or execute them. "
    "Regex policy checks are bypassable (for example, window['ev'+'al']) and are not a security boundary."
)


def _serialize_userscript(script: Userscript) -> dict[str, Any]:
    """Expose stored policy metadata without implying script execution or reporting fake run counters."""
    return {
        "id": script.id,
        "name": script.name,
        "namespace": script.namespace,
        "version": script.version,
        "description": script.description,
        "match_patterns": script.match_patterns,
        "allowed_roles": script.allowed_roles,
        "allowed_agent_ids": script.allowed_agent_ids,
        "code": script.code,
        "is_enabled": script.is_enabled,
        "audit_status": script.audit_status,
        "created_at": script.created_at,
        "storage_only": True,
        "execution_supported": False,
        "policy_checks_security_boundary": False,
        "policy_notice": USERSCRIPT_STORAGE_ONLY_NOTICE,
    }


@router.get("/userscripts")
async def list_userscripts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Userscript).order_by(Userscript.created_at.desc()))).scalars().all()
    return [_serialize_userscript(row) for row in rows]


@router.post("/userscripts", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def create_userscript(
    body: UserscriptCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok, msg = validate_userscript_policy(
        body.code,
        body.match_patterns,
        allowed_roles=body.allowed_roles,
        allowed_agent_ids=body.allowed_agent_ids,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    us = Userscript(
        id=f"us-{uuid.uuid4().hex[:8]}",
        name=body.name,
        description=body.description,
        match_patterns=body.match_patterns,
        allowed_roles=body.allowed_roles,
        allowed_agent_ids=body.allowed_agent_ids,
        code=body.code,
        is_enabled=True,
        audit_status="approved",
    )
    db.add(us)
    await db.commit()
    await db.refresh(us)
    return _serialize_userscript(us)


@router.patch("/userscripts/{script_id}/toggle", dependencies=[Depends(require_role("admin", "operator"))])
async def toggle_userscript(
    script_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    us = await db.get(Userscript, script_id)
    if not us:
        raise HTTPException(status_code=404, detail="Userscript not found")
    us.is_enabled = not us.is_enabled
    await db.commit()
    await db.refresh(us)
    return _serialize_userscript(us)
