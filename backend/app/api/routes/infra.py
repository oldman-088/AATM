from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.browser.engines import BROWSER_ENGINE_REGISTRY
from app.browser.manager import browser_manager, detect_playwright_capabilities
from app.core.config import get_settings
from app.db.session import get_db
from app.models import Agent, AIProvider, AuditLog, BrowserProfile, BrowserSession, ProviderUsageLog, User
from app.providers.adapters import build_adapter
from app.providers.base import ProviderRequest
from app.providers.base import validate_credential_ref as validate_provider_credential_ref
from app.providers.router import get_model_configuration_status, provider_router
from app.schemas.common import (
    BrowserCreateRequest,
    BrowserExtractTextRequest,
    BrowserLaunchRequest,
    BrowserNavigateRequest,
    BrowserWorkflowRequest,
    ProviderCreateOrUpdate,
    ProviderTestRequest,
    ProviderUsageResponse,
    ProviderWizardConfigureRequest,
    ProviderWizardTestRequest,
)
from app.schemas.common import (
    validate_credential_ref as validate_credential_ref_format,
)

router = APIRouter(tags=["providers-browsers"], dependencies=[Depends(get_current_user)])


def _serialize_provider(p: AIProvider) -> dict:
    credential_configured, _ = validate_provider_credential_ref(p.credential_ref)
    return {
        "id": p.id,
        "name": p.name,
        "provider_type": p.provider_type,
        "endpoint_url": p.endpoint_url,
        "credential_status": "configured" if credential_configured else "not_configured",
        "credential_ref": p.credential_ref,
        "default_model": p.default_model,
        "available_models": p.available_models,
        "priority": p.priority,
        "is_enabled": p.is_enabled,
        "health_status": p.health_status if credential_configured else "degraded",
        "latency_ms": p.latency_ms,
        "daily_limit_usd": p.daily_limit_usd,
        "last_checked_at": p.last_checked_at,
    }


def _serialize_browser(b: BrowserProfile) -> dict:
    live_context = browser_manager.has_live_context(b.id)
    status = "unavailable" if b.status == "running" and not live_context else b.status
    engine_mode = b.engine_mode if live_context or b.status != "running" else "unavailable"
    return {
        "id": b.id,
        "name": b.name,
        "agent_id": b.agent_id,
        "profile_dir": b.profile_dir,
        "mode": b.mode,
        "status": status,
        "live_context": live_context,
        "lock_owner": b.lock_owner,
        "current_url": b.current_url,
        "page_title": b.page_title,
        "viewport_width": b.viewport_width,
        "viewport_height": b.viewport_height,
        "crash_count": b.crash_count,
        "memory_mb": None,
        "memory_status": "not_measured",
        "engine_mode": engine_mode,
        "last_snapshot_svg": b.last_snapshot_svg,
        "last_heartbeat": b.last_heartbeat,
    }


@router.get("/providers")
async def list_providers(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(AIProvider).order_by(AIProvider.priority.asc()))
    return [_serialize_provider(p) for p in res.scalars().all()]


@router.post("/providers", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def upsert_provider(
    body: ProviderCreateOrUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        validate_credential_ref_format(body.credential_ref)
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


@router.get("/providers/wizard/presets")
async def get_provider_wizard_presets(db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    model_status = await get_model_configuration_status(db)
    return {
        "model_status": model_status,
        "options": [
            {
                "id": "anthropic-claude",
                "provider_type": "anthropic_claude",
                "title": "Anthropic Claude API",
                "endpoint_url": "https://api.anthropic.com/v1",
                "credential_ref": settings.ANTHROPIC_API_KEY_REF or "env:ANTHROPIC_API_KEY",
                "default_model": "claude-sonnet-4-5",
                "suggested_models": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"],
            },
            {
                "id": "openai-compatible",
                "provider_type": "openai_compatible",
                "title": "OpenAI-Compatible (Ollama / OpenRouter / LiteLLM / vLLM / LM Studio)",
                "endpoint_url": "http://127.0.0.1:11434/v1",
                "credential_ref": "env:OLLAMA_API_KEY",
                "default_model": "qwen2.5-coder:7b",
                "suggested_models": [
                    "qwen2.5-coder:7b",
                    "llama3.1:8b",
                    "deepseek-coder-v2",
                    "gpt-4o-mini",
                    "gpt-4o",
                ],
                "sub_presets": [
                    {
                        "id": "ollama",
                        "label": "Ollama (Local)",
                        "endpoint_url": "http://127.0.0.1:11434/v1",
                        "credential_ref": "env:OLLAMA_API_KEY",
                        "default_model": "qwen2.5-coder:7b",
                    },
                    {
                        "id": "openrouter",
                        "label": "OpenRouter",
                        "endpoint_url": "https://openrouter.ai/api/v1",
                        "credential_ref": "env:OPENROUTER_API_KEY",
                        "default_model": "anthropic/claude-sonnet-4",
                    },
                    {
                        "id": "litellm",
                        "label": "LiteLLM Proxy",
                        "endpoint_url": "http://127.0.0.1:4000/v1",
                        "credential_ref": "env:LITELLM_API_KEY",
                        "default_model": "gpt-4o-mini",
                    },
                    {
                        "id": "vllm",
                        "label": "vLLM / LM Studio",
                        "endpoint_url": "http://127.0.0.1:1234/v1",
                        "credential_ref": "env:OPENAI_API_KEY",
                        "default_model": "local-model",
                    },
                ],
            },
            {
                "id": "arena-local-bridge",
                "provider_type": "arena_local_bridge",
                "title": "Arena Local Bridge (with upstream)",
                "endpoint_url": settings.ARENA_BRIDGE_URL,
                "credential_ref": "env:ARENA_BRIDGE_TOKEN",
                "default_model": "arena-bridge-default",
                "suggested_models": ["arena-bridge-default", "arena-reasoning-pro", "arena-fast"],
            },
        ],
    }


@router.post("/providers/wizard/test-connection", dependencies=[Depends(require_role("admin", "operator"))])
async def test_provider_wizard_connection(body: ProviderWizardTestRequest):
    try:
        validate_credential_ref_format(body.credential_ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    adapter = build_adapter(
        body.provider_type,
        f"wizard-{body.provider_type}",
        body.endpoint_url,
        body.credential_ref,
        body.model,
    )
    resp = await adapter.generate(
        ProviderRequest(
            prompt="Reply with ok.",
            system_prompt="",
            model=body.model,
            max_tokens=1,
            temperature=0.0,
            dry_run=False,
            allow_diagnostic_fallback=False,
        )
    )
    return {
        "success": resp.success,
        "provider_type": body.provider_type,
        "model": resp.model,
        "latency_ms": resp.latency_ms,
        "pricing_known": resp.pricing_known,
        "detail": (
            f"Connected to {resp.model} in {resp.latency_ms} ms"
            if resp.success
            else (resp.error_message or "Connection check failed")
        ),
        "error_message": resp.error_message,
        "response_excerpt": resp.content[:120] if resp.success else "",
    }


@router.post("/providers/wizard/configure", status_code=200, dependencies=[Depends(require_role("admin", "operator"))])
async def configure_provider_wizard(
    body: ProviderWizardConfigureRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        validate_credential_ref_format(body.credential_ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    default_ids = {
        "anthropic_claude": "anthropic-claude",
        "openai_compatible": "openai-compatible",
        "arena_local_bridge": "arena-local-bridge",
    }
    default_names = {
        "anthropic_claude": "Anthropic Claude API",
        "openai_compatible": "OpenAI-Compatible Provider",
        "arena_local_bridge": "Arena Local Bridge",
    }
    pid = body.id or default_ids[body.provider_type]
    name = (body.name or "").strip() or default_names[body.provider_type]
    models = list(dict.fromkeys([body.default_model, *(body.available_models or [])]))

    existing_all = list((await db.execute(select(AIProvider))).scalars().all())
    for item in existing_all:
        if item.id != pid and item.priority <= 1:
            item.priority = max(2, item.priority + 1)

    provider = await db.get(AIProvider, pid)
    if provider is None:
        provider = AIProvider(
            id=pid,
            name=name,
            provider_type=body.provider_type,
            endpoint_url=body.endpoint_url,
            credential_ref=body.credential_ref,
            default_model=body.default_model,
            available_models=models,
            priority=1,
            is_enabled=True,
            daily_limit_usd=body.daily_limit_usd,
        )
        db.add(provider)
    else:
        provider.name = name
        provider.provider_type = body.provider_type
        provider.endpoint_url = body.endpoint_url
        provider.credential_ref = body.credential_ref
        provider.default_model = body.default_model
        provider.available_models = models
        provider.priority = 1
        provider.is_enabled = True
        provider.daily_limit_usd = body.daily_limit_usd

    await db.flush()
    updated_agents: list[str] = []
    if body.assign_to_manager or body.assign_to_workers:
        agents = list((await db.execute(select(Agent))).scalars().all())
        for ag in agents:
            if (body.assign_to_manager and ag.role == "manager") or (
                body.assign_to_workers and ag.role == "worker"
            ):
                ag.provider_id = provider.id
                ag.model_name = body.default_model
                updated_agents.append(ag.id)

    db.add(
        AuditLog(
            actor=user.email,
            action="provider.wizard_configure",
            resource_type="ai_provider",
            resource_id=provider.id,
            details={
                "provider_type": provider.provider_type,
                "default_model": provider.default_model,
                "credential_ref": provider.credential_ref,
                "updated_agents": updated_agents,
            },
        )
    )
    await db.commit()
    await db.refresh(provider)
    health = await provider_router.check_provider_health(db, provider)
    model_status = await get_model_configuration_status(db)
    return {
        "provider": _serialize_provider(provider),
        "health": {
            "status": health.status,
            "latency_ms": health.latency_ms,
            "detail": health.detail,
            "credentials_configured": health.credentials_configured,
        },
        "model_status": model_status,
        "updated_agents": updated_agents,
    }


@router.post("/providers/health-check", dependencies=[Depends(require_role("admin", "operator"))])
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


@router.post("/providers/{provider_id}/test", dependencies=[Depends(require_role("admin", "operator"))])
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
        "input_tokens_estimated": resp.input_tokens_estimated,
        "output_tokens_estimated": resp.output_tokens_estimated,
        "estimated_cost_usd": resp.estimated_cost_usd,
        "confirmed_cost_usd": resp.confirmed_cost_usd,
        "retryable": resp.retryable,
        "retry_after_s": resp.retry_after_s,
        "latency_ms": resp.latency_ms,
        "error_message": resp.error_message,
    }


@router.get("/providers/usage", response_model=ProviderUsageResponse)
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
    has_unknown_pricing = any(not bool(lg.pricing_known) for lg in logs)

    return {
        "budget_summary": budget_summary,
        "summary": {
            "total_input_tokens": int(in_tok or 0),
            "total_output_tokens": int(out_tok or 0),
            "total_estimated_cost_usd": round(float(total_cost or 0.0), 6),
            "pricing_known": not has_unknown_pricing if logs else True,
            "has_unknown_pricing": has_unknown_pricing,
        },
        "logs": [
            {
                "id": lg.id,
                "provider_id": lg.provider_id,
                "agent_id": lg.agent_id,
                "model_name": lg.model_name,
                "input_tokens": lg.input_tokens,
                "output_tokens": lg.output_tokens,
                "input_tokens_estimated": lg.input_tokens_estimated,
                "output_tokens_estimated": lg.output_tokens_estimated,
                "estimated_cost_usd": lg.estimated_cost_usd,
                "confirmed_cost_usd": lg.confirmed_cost_usd,
                "pricing_known": bool(lg.pricing_known),
                "latency_ms": lg.latency_ms,
                "success": lg.success,
                "error_message": lg.error_message,
                "created_at": lg.created_at,
            }
            for lg in logs
        ],
    }


@router.post("/providers/sessions/recover")
async def recover_degraded_provider_sessions(
    user: User = Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    return await provider_router.recover_degraded_sessions(db)


@router.get("/browsers/capabilities")
async def browser_capabilities():
    return detect_playwright_capabilities()


@router.get("/browsers")
async def list_browsers(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(BrowserProfile).order_by(BrowserProfile.id.asc()))
    return [_serialize_browser(b) for b in res.scalars().all()]


@router.post("/browsers", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
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


@router.post("/browsers/{profile_id}/launch", dependencies=[Depends(require_role("admin", "operator"))])
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


@router.post("/browsers/{profile_id}/navigate", dependencies=[Depends(require_role("admin", "operator"))])
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


@router.post("/browsers/{profile_id}/workflow", dependencies=[Depends(require_role("admin", "operator"))])
async def execute_browser_workflow(
    profile_id: str,
    body: BrowserWorkflowRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    profile = await db.get(BrowserProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Browser profile not found")
    engine = BROWSER_ENGINE_REGISTRY.get(body.engine)
    if engine is None:
        raise HTTPException(status_code=400, detail=f"Unsupported browser workflow engine '{body.engine}'.")
    workflow_actor = user.email
    if body.agent_id is not None:
        if body.agent_id != profile.agent_id:
            raise HTTPException(status_code=403, detail="Agent ID does not match the browser profile")
        workflow_actor = body.agent_id
    try:
        result = await engine.execute_workflow(
            db,
            profile,
            body.steps,
            manager=browser_manager,
            actor=workflow_actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if result.get("status") == "rejected":
        raise HTTPException(status_code=400, detail=result.get("reason") or "Workflow rejected by browser policy")
    return result


@router.post("/browsers/{profile_id}/extract-text", dependencies=[Depends(require_role("admin", "operator"))])
async def extract_browser_text(
    profile_id: str,
    body: BrowserExtractTextRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    profile = await db.get(BrowserProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Browser profile not found")
    try:
        return await browser_manager.extract_text(db, profile, body.selector, max_chars=body.max_chars)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/browsers/{profile_id}/stop", dependencies=[Depends(require_role("admin", "operator"))])
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


@router.post("/browsers/{profile_id}/recover", dependencies=[Depends(require_role("admin", "operator"))])
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
