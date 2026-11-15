from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.browser.manager import browser_manager, detect_playwright_capabilities
from app.core.config import get_settings
from app.core.security import mask_credential_ref
from app.db.session import get_db
from app.models import AIProvider, BrowserProfile, BrowserSession, ProviderUsageLog, User
from app.providers.base import ProviderRequest
from app.providers.router import provider_router
from app.schemas.common import (
    BrowserCreateRequest,
    BrowserLaunchRequest,
    BrowserNavigateRequest,
    ProviderCreateOrUpdate,
    ProviderTestRequest,
    validate_credential_ref,
)

router = APIRouter(tags=["providers-browsers"])


def _serialize_provider(p: AIProvider) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "provider_type": p.provider_type,
        "endpoint_url": p.endpoint_url,
        "credential_status": mask_credential_ref(p.credential_ref),
        "credential_ref": p.credential_ref,
        "default_model": p.default_model,
        "available_models": p.available_models,
        "priority": p.priority,
        "is_enabled": p.is_enabled,
        "health_status": p.health_status,
        "latency_ms": p.latency_ms,
        "daily_limit_usd": p.daily_limit_usd,
        "last_checked_at": p.last_checked_at,
    }


def _serialize_browser(b: BrowserProfile) -> dict:
    return {
        "id": b.id,
        "name": b.name,
        "agent_id": b.agent_id,
        "profile_dir": b.profile_dir,
        "mode": b.mode,
        "status": b.status,
        "lock_owner": b.lock_owner,
        "current_url": b.current_url,
        "page_title": b.page_title,
        "viewport_width": b.viewport_width,
        "viewport_height": b.viewport_height,
        "crash_count": b.crash_count,
        "memory_mb": b.memory_mb,
        "engine_mode": b.engine_mode,
        "last_snapshot_svg": b.last_snapshot_svg,
        "last_heartbeat": b.last_heartbeat,
    }


@router.get("/providers")
async def list_providers(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(AIProvider).order_by(AIProvider.priority.asc()))
    return [_serialize_provider(p) for p in res.scalars().all()]


@router.post("/providers", status_code=201)
async def upsert_provider(
    body: ProviderCreateOrUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        validate_credential_ref(body.credential_ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    pid = body.id or body.name.lower().replace(" ", "-")
    existing = await db.get(AIProvider, pid)
    if existing:
        existing.name = body.name
        existing.provider_type = body.provider_type
        existing.endpoint_url = body.endpoint_url
        existing.credential_ref = body.credential_ref
        existing.default_model = body.default_model
        existing.available_models = body.available_models or [body.default_model]
        existing.priority = body.priority
        existing.is_enabled = body.is_enabled
        existing.daily_limit_usd = body.daily_limit_usd
        p = existing
    else:
        p = AIProvider(
            id=pid,
            name=body.name,
            provider_type=body.provider_type,
            endpoint_url=body.endpoint_url,
            credential_ref=body.credential_ref,
            default_model=body.default_model,
            available_models=body.available_models or [body.default_model],
            priority=body.priority,
            is_enabled=body.is_enabled,
            daily_limit_usd=body.daily_limit_usd,
        )
        db.add(p)
    await db.commit()
    await db.refresh(p)
    return _serialize_provider(p)


@router.post("/providers/health-check")
async def health_check_all_providers(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    results = await provider_router.check_all_health(db)
    return [
        {
            "provider_id": r.provider_id,
            "status": r.status,
            "latency_ms": r.latency_ms,
            "detail": r.detail,
            "credentials_configured": r.credentials_configured,
        }
        for r in results
    ]


@router.post("/providers/{provider_id}/test")
async def test_provider(
    provider_id: str,
    body: ProviderTestRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(AIProvider, provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    request_metadata = {"session_id": body.session_id} if body.session_id is not None else {}
    resp = await provider_router.route_and_execute(
        db,
        ProviderRequest(
            prompt=body.prompt,
            model=body.model,
            dry_run=body.dry_run,
            agent_id=body.agent_id or "manager-0",
            metadata=request_metadata,
        ),
        preferred_provider_id=provider_id,
    )
    return {
        "provider_id": resp.provider_id,
        "model": resp.model,
        "success": resp.success,
        "content": resp.content,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "estimated_cost_usd": resp.estimated_cost_usd,
        "latency_ms": resp.latency_ms,
        "error_message": resp.error_message,
    }


@router.get("/providers/usage")
async def get_provider_usage(db: AsyncSession = Depends(get_db)):
    logs = list((await db.execute(select(ProviderUsageLog).order_by(ProviderUsageLog.created_at.desc()).limit(50))).scalars().all())
    tot_res = await db.execute(
        select(
            func.coalesce(func.sum(ProviderUsageLog.input_tokens), 0),
            func.coalesce(func.sum(ProviderUsageLog.output_tokens), 0),
            func.coalesce(func.sum(ProviderUsageLog.estimated_cost_usd), 0.0),
        )
    )
    in_tok, out_tok, total_cost = tot_res.one()

    budget_summary = await provider_router.get_spend_summary(db)

    return {
        "budget_summary": budget_summary,
        "summary": {
            "total_input_tokens": int(in_tok or 0),
            "total_output_tokens": int(out_tok or 0),
            "total_estimated_cost_usd": round(float(total_cost or 0.0), 6),
        },
        "logs": [
            {
                "id": lg.id,
                "provider_id": lg.provider_id,
                "agent_id": lg.agent_id,
                "model_name": lg.model_name,
                "input_tokens": lg.input_tokens,
                "output_tokens": lg.output_tokens,
                "estimated_cost_usd": lg.estimated_cost_usd,
                "latency_ms": lg.latency_ms,
                "success": lg.success,
                "error_message": lg.error_message,
                "created_at": lg.created_at,
            }
            for lg in logs
        ],
    }


@router.get("/browsers/capabilities")
async def browser_capabilities():
    return detect_playwright_capabilities()


@router.get("/browsers")
async def list_browsers(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(BrowserProfile).order_by(BrowserProfile.id.asc()))
    return [_serialize_browser(b) for b in res.scalars().all()]


@router.post("/browsers", status_code=201)
async def create_browser_profile(
    body: BrowserCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if await db.get(BrowserProfile, body.id):
        raise HTTPException(status_code=409, detail="Browser profile ID already exists")
    settings = get_settings()
    bp = BrowserProfile(
        id=body.id,
        name=body.name,
        agent_id=body.agent_id,
        profile_dir=str(settings.BROWSER_PROFILES_DIR / body.id),
        mode=body.mode,
        current_url=body.current_url,
        page_title=body.name,
    )
    db.add(bp)
    await db.commit()
    await db.refresh(bp)
    return _serialize_browser(bp)


@router.post("/browsers/{profile_id}/launch")
async def launch_browser(
    profile_id: str,
    body: BrowserLaunchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    bp = await db.get(BrowserProfile, profile_id)
    if not bp:
        raise HTTPException(status_code=404, detail="Browser profile not found")
    try:
        updated = await browser_manager.launch_profile(db, bp, owner=body.owner, mode=body.mode, url=body.url)
        return _serialize_browser(updated)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/browsers/{profile_id}/navigate")
async def navigate_browser(
    profile_id: str,
    body: BrowserNavigateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    bp = await db.get(BrowserProfile, profile_id)
    if not bp:
        raise HTTPException(status_code=404, detail="Browser profile not found")
    try:
        updated = await browser_manager.navigate_profile(db, bp, body.url, body.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _serialize_browser(updated)


@router.post("/browsers/{profile_id}/stop")
async def stop_browser(
    profile_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    bp = await db.get(BrowserProfile, profile_id)
    if not bp:
        raise HTTPException(status_code=404, detail="Browser profile not found")
    updated = await browser_manager.stop_profile(db, bp, actor=user.email)
    return _serialize_browser(updated)


@router.post("/browsers/{profile_id}/recover")
async def crash_and_recover_browser(
    profile_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    bp = await db.get(BrowserProfile, profile_id)
    if not bp:
        raise HTTPException(status_code=404, detail="Browser profile not found")
    updated = await browser_manager.simulate_crash_and_recover(db, bp, recover=True)
    return _serialize_browser(updated)


@router.get("/browsers/{profile_id}/sessions")
async def list_browser_sessions(profile_id: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(BrowserSession).where(BrowserSession.profile_id == profile_id).order_by(BrowserSession.started_at.desc()).limit(20)
    )
    return list(res.scalars().all())
