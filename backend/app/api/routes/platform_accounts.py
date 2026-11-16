"""Provider accounts and isolated provider sessions."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.security import mask_credential_ref
from app.db.base import utcnow
from app.db.session import get_db
from app.models import (
    AIProvider,
    AuditLog,
    ProviderAccount,
    ProviderSession,
    User,
)
from app.providers.credential_policy import CredentialPolicyError, validate_provider_credential_ref
from app.schemas.common import (
    ProviderAccountCreate,
)
from app.schemas.common import validate_credential_ref as validate_credential_ref_format

router = APIRouter(tags=["enterprise-platform"], dependencies=[Depends(get_current_user)])


@router.get("/accounts")
async def list_provider_accounts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ProviderAccount).order_by(ProviderAccount.created_at.desc()))).scalars().all()
    return [
        {
            "id": a.id,
            "provider_id": a.provider_id,
            "label": a.label,
            "account_identifier": a.account_identifier,
            "allocation_mode": a.allocation_mode,
            "max_employees": a.max_employees,
            "credential_status": mask_credential_ref(a.credential_ref),
            "status": a.status,
            "created_at": a.created_at,
        }
        for a in rows
    ]


@router.post("/accounts", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def create_provider_account(
    body: ProviderAccountCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    provider = await db.get(AIProvider, body.provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    try:
        validate_credential_ref_format(body.credential_ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    try:
        credential_ref = validate_provider_credential_ref(
            body.credential_ref, provider_type=provider.provider_type, endpoint_url=provider.endpoint_url
        )
    except CredentialPolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from None
    acc = ProviderAccount(
        id=f"acct-{uuid.uuid4().hex[:8]}",
        provider_id=body.provider_id,
        label=body.label,
        account_identifier=body.account_identifier,
        allocation_mode=body.allocation_mode,
        max_employees=body.max_employees,
        credential_ref=credential_ref,
        status="active",
        last_verified_at=utcnow(),
    )
    db.add(acc)
    db.add(
        AuditLog(
            actor=user.email,
            action="provider_account.create",
            resource_type="provider_account",
            resource_id=acc.id,
            details={"provider_id": body.provider_id, "credential_ref": credential_ref},
        )
    )
    await db.commit()
    await db.refresh(acc)
    return acc


@router.get("/sessions")
async def list_provider_sessions(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ProviderSession).order_by(ProviderSession.created_at.desc()))).scalars().all()
    return list(rows)


@router.post("/sessions/{session_id}/recover", dependencies=[Depends(require_role("admin", "operator"))])
async def recover_provider_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    sess = await db.get(ProviderSession, session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Provider session not found")
    sess.status = "active"
    sess.health_score = 100.0
    sess.last_error = None
    await db.commit()
    await db.refresh(sess)
    return sess
