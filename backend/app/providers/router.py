import asyncio
import math
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.models import (
    Agent,
    AIProvider,
    AuditLog,
    ProviderAccount,
    ProviderSession,
    ProviderUsageLog,
    SystemSetting,
)
from app.providers.adapters import ArenaLocalBridgeAdapter, build_adapter
from app.providers.base import (
    BaseProviderAdapter,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
    ProviderStreamChunk,
    validate_credential_ref,
)


class ProviderRouter:
    """Routes AI requests across enabled providers with credential indirection, daily/monthly budget caps, failover & usage logging."""

    async def check_provider_health(self, db: AsyncSession, provider: AIProvider) -> ProviderHealth:
        adapter = build_adapter(
            provider.provider_type,
            provider.id,
            provider.endpoint_url,
            provider.credential_ref,
            provider.default_model,
        )
        health = await adapter.health_check()
        provider.health_status = health.status
        provider.latency_ms = health.latency_ms
        provider.last_checked_at = utcnow()
        await db.commit()
        return health

    async def check_all_health(self, db: AsyncSession) -> list[ProviderHealth]:
        res = await db.execute(select(AIProvider).order_by(AIProvider.priority.asc()))
        providers = list(res.scalars().all())
        results = []
        for p in providers:
            results.append(await self.check_provider_health(db, p))
        return results

    async def get_daily_spend(self, db: AsyncSession, provider_id: str | None = None) -> float:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        stmt = select(func.coalesce(func.sum(ProviderUsageLog.estimated_cost_usd), 0.0)).where(
            ProviderUsageLog.created_at >= since,
        )
        if provider_id is not None:
            stmt = stmt.where(ProviderUsageLog.provider_id == provider_id)
        res = await db.execute(stmt)
        return round(float(res.scalar_one() or 0.0), 6)

    async def get_monthly_spend(self, db: AsyncSession, provider_id: str | None = None) -> float:
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        stmt = select(func.coalesce(func.sum(ProviderUsageLog.estimated_cost_usd), 0.0)).where(
            ProviderUsageLog.created_at >= month_start,
        )
        if provider_id is not None:
            stmt = stmt.where(ProviderUsageLog.provider_id == provider_id)
        res = await db.execute(stmt)
        return round(float(res.scalar_one() or 0.0), 6)

    async def get_budget_limits(self, db: AsyncSession) -> dict[str, float]:
        settings = get_settings()
        daily_limit = float(settings.DAILY_BUDGET_USD)
        monthly_limit = float(settings.MONTHLY_BUDGET_USD)
        approval_threshold = 5.0

        budget_setting = await db.get(SystemSetting, "budgets")
        if budget_setting and isinstance(budget_setting.value, dict):
            if budget_setting.value.get("daily_limit_usd") is not None:
                daily_limit = float(budget_setting.value["daily_limit_usd"])
            if budget_setting.value.get("monthly_limit_usd") is not None:
                monthly_limit = float(budget_setting.value["monthly_limit_usd"])
            if budget_setting.value.get("require_approval_above_usd") is not None:
                approval_threshold = float(budget_setting.value["require_approval_above_usd"])

        return {
            "daily_limit_usd": daily_limit,
            "monthly_limit_usd": monthly_limit,
            "require_approval_above_usd": approval_threshold,
        }

    async def get_spend_summary(self, db: AsyncSession) -> dict[str, float]:
        limits = await self.get_budget_limits(db)
        daily_spend = await self.get_daily_spend(db)
        monthly_spend = await self.get_monthly_spend(db)
        return {
            "global_daily_spend_usd": daily_spend,
            "global_monthly_spend_usd": monthly_spend,
            "daily_limit_usd": limits["daily_limit_usd"],
            "monthly_limit_usd": limits["monthly_limit_usd"],
            "daily_remaining_usd": round(max(0.0, limits["daily_limit_usd"] - daily_spend), 6),
            "monthly_remaining_usd": round(max(0.0, limits["monthly_limit_usd"] - monthly_spend), 6),
        }

    async def check_spend_caps(self, db: AsyncSession, provider: AIProvider) -> tuple[bool, str | None]:
        limits = await self.get_budget_limits(db)

        global_monthly = await self.get_monthly_spend(db)
        if global_monthly >= limits["monthly_limit_usd"]:
            return False, f"Global monthly budget cap (${limits['monthly_limit_usd']:.2f}) reached."

        provider_monthly_cap = None
        if isinstance(provider.config, dict) and provider.config.get("monthly_limit_usd") is not None:
            try:
                provider_monthly_cap = float(provider.config["monthly_limit_usd"])
            except (TypeError, ValueError):
                provider_monthly_cap = None
        if provider_monthly_cap is not None and provider_monthly_cap >= 0:
            provider_monthly = await self.get_monthly_spend(db, provider.id)
            if provider_monthly >= provider_monthly_cap:
                return False, f"Provider {provider.id} reached monthly budget cap (${provider_monthly_cap:.2f})."

        global_daily = await self.get_daily_spend(db)
        if global_daily >= limits["daily_limit_usd"]:
            return False, f"Global daily budget cap (${limits['daily_limit_usd']:.2f}) reached."

        provider_daily = await self.get_daily_spend(db, provider.id)
        if provider_daily >= provider.daily_limit_usd:
            return False, f"Provider {provider.id} reached daily budget cap (${provider.daily_limit_usd:.2f})."

        return True, None

    @staticmethod
    def _select_model(provider: AIProvider, requested_model: str | None) -> str:
        if requested_model:
            if not provider.available_models or requested_model in provider.available_models:
                return requested_model
        return provider.default_model

    @staticmethod
    def _update_session_health(
        employee_session: ProviderSession | None,
        *,
        success: bool,
        error_message: str | None,
        transport_unavailable: bool,
    ) -> None:
        if employee_session is None:
            return
        if success:
            employee_session.message_count += 1
            employee_session.last_message_at = utcnow()
            employee_session.last_error = None
            employee_session.health_score = min(100.0, round(float(employee_session.health_score) + 10.0, 2))
            if employee_session.status == "degraded" and employee_session.health_score >= 60.0:
                employee_session.status = "active"
        else:
            penalty = 5.0 if transport_unavailable else 15.0
            employee_session.error_count += 1
            employee_session.last_error = error_message
            employee_session.health_score = max(0.0, round(float(employee_session.health_score) - penalty, 2))
            if employee_session.health_score < 50.0 and employee_session.status == "active":
                employee_session.status = "degraded"

    @staticmethod
    def _max_retries() -> int:
        raw = os.environ.get("PROVIDER_MAX_RETRIES")
        if raw is None:
            raw = get_settings().PROVIDER_MAX_RETRIES
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def _retry_delay(response: ProviderResponse | ProviderStreamChunk) -> float:
        raw = response.retry_after_s
        if raw is None:
            return 0.0
        try:
            delay = float(raw)
            raw_cap = os.environ.get("PROVIDER_RETRY_AFTER_MAX_SECONDS")
            cap = float(raw_cap) if raw_cap is not None else float(get_settings().PROVIDER_RETRY_AFTER_MAX_SECONDS)
        except (AttributeError, TypeError, ValueError):
            return 0.0
        if not math.isfinite(delay):
            return max(0.0, cap) if delay > 0 else 0.0
        return min(max(0.0, delay), max(0.0, cap))

    async def _record_usage_attempt(
        self,
        db: AsyncSession,
        provider: AIProvider,
        req: ProviderRequest,
        response: ProviderResponse | ProviderStreamChunk,
        *,
        attempt: int,
        streaming: bool = False,
    ) -> float:
        """Persist each real provider attempt; never log dry-run placeholders as usage."""
        if req.dry_run or response.metadata.get("dry_run"):
            return 0.0

        reported_usage = bool(response.metadata.get("confirmed_usage"))
        cost = float(response.estimated_cost_usd) if response.success or reported_usage else 0.0
        if not math.isfinite(cost) or cost < 0:
            cost = 0.0
        confirmed_cost = response.confirmed_cost_usd
        if confirmed_cost is not None and (not math.isfinite(float(confirmed_cost)) or float(confirmed_cost) < 0):
            confirmed_cost = None
        db.add(
            ProviderUsageLog(
                provider_id=provider.id,
                agent_id=req.agent_id,
                task_id=req.task_id,
                model_name=response.model,
                input_tokens=max(0, int(response.input_tokens)),
                output_tokens=max(0, int(response.output_tokens)),
                input_tokens_estimated=response.input_tokens_estimated,
                output_tokens_estimated=response.output_tokens_estimated,
                estimated_cost_usd=cost,
                confirmed_cost_usd=confirmed_cost,
                pricing_known=bool(response.pricing_known or confirmed_cost is not None),
                latency_ms=max(0.0, float(response.latency_ms)),
                success=response.success,
                error_message=response.error_message,
            )
        )
        if response.metadata.get("transport_unavailable"):
            provider.health_status = "offline"
        else:
            provider.health_status = "healthy" if response.success else "degraded"
        provider.latency_ms = response.latency_ms
        provider.last_checked_at = utcnow()
        await db.commit()
        await event_bus.emit(
            "provider.usage",
            {
                "provider_id": provider.id,
                "model": response.model,
                "success": response.success,
                "estimated_cost_usd": cost,
                "confirmed_cost_usd": confirmed_cost,
                "retryable": response.retryable,
                "attempt": attempt,
                "stream": streaming,
                "session_id": req.metadata.get("session_id"),
                "account_id": req.metadata.get("account_id"),
            },
        )
        return cost

    async def _resolve_employee_session_context(
        self,
        db: AsyncSession,
        req: ProviderRequest,
    ) -> tuple[dict[str, Any], ProviderSession | None]:
        """Resolve per-employee ProviderSession.id & account_id into request metadata, rejecting cross-account overrides."""
        merged_meta: dict[str, Any] = dict(req.metadata or {})
        session: ProviderSession | None = None
        agent: Agent | None = None

        if req.agent_id:
            merged_meta.setdefault("agent_id", req.agent_id)
            agent = await db.get(Agent, req.agent_id)

        explicit_session_id = str(merged_meta.get("session_id") or "").strip() or None
        explicit_account_id = str(merged_meta.get("account_id") or "").strip() or None

        if explicit_session_id:
            session = await db.get(ProviderSession, explicit_session_id)
            if session is not None:
                if agent and agent.provider_account_id and session.account_id and session.account_id != agent.provider_account_id:
                    raise ValueError(
                        f"Cross-account session override denied: session '{explicit_session_id}' belongs to account '{session.account_id}', not employee '{agent.id}' account '{agent.provider_account_id}'."
                    )
                if explicit_account_id and session.account_id and session.account_id != explicit_account_id:
                    raise ValueError(
                        f"Cross-account session override denied: session '{explicit_session_id}' belongs to account '{session.account_id}', not requested account '{explicit_account_id}'."
                    )
        elif agent and agent.provider_session_id:
            session = await db.get(ProviderSession, agent.provider_session_id)
        elif req.agent_id:
            sess_res = await db.execute(
                select(ProviderSession)
                .where(ProviderSession.agent_id == req.agent_id, ProviderSession.status != "closed")
                .order_by(ProviderSession.created_at.desc())
                .limit(1)
            )
            session = sess_res.scalar_one_or_none()

        if session is not None:
            merged_meta.setdefault("session_id", session.id)
            if session.account_id:
                merged_meta.setdefault("account_id", session.account_id)
            if session.conversation_id:
                merged_meta.setdefault("conversation_id", session.conversation_id)
            if session.project_id:
                merged_meta.setdefault("project_id", session.project_id)
        elif agent and agent.provider_account_id:
            merged_meta.setdefault("account_id", agent.provider_account_id)

        return merged_meta, session

    async def route_and_execute(
        self,
        db: AsyncSession,
        req: ProviderRequest,
        preferred_provider_id: str | None = None,
    ) -> ProviderResponse:
        if req.dry_run:
            try:
                await self._resolve_employee_session_context(db, req)
            except ValueError as exc:
                return ProviderResponse(
                    provider_id=preferred_provider_id or "none",
                    model=req.model or "none",
                    content="",
                    input_tokens=0,
                    output_tokens=0,
                    estimated_cost_usd=0.0,
                    latency_ms=0.0,
                    success=False,
                    error_message=str(exc),
                    metadata={"cross_account_denied": True},
                )
            return ProviderResponse(
                provider_id=preferred_provider_id or "none",
                model=req.model or "none",
                content="Dry run: no provider request was sent.",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=0.0,
                success=True,
                metadata={"dry_run": True, "mode": "dry_run"},
            )

        res = await db.execute(
            select(AIProvider).where(AIProvider.is_enabled.is_(True)).order_by(AIProvider.priority.asc())
        )
        providers = list(res.scalars().all())
        if preferred_provider_id:
            providers.sort(key=lambda p: (0 if p.id == preferred_provider_id else 1, p.priority))

        try:
            session_meta, employee_session = await self._resolve_employee_session_context(db, req)
        except ValueError as exc:
            return ProviderResponse(
                provider_id=preferred_provider_id or "none",
                model=req.model or "none",
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=0.0,
                success=False,
                error_message=str(exc),
                metadata={"cross_account_denied": True},
            )

        last_err = "No enabled AI providers available."
        budget_blocked = False
        budget_error: str | None = None
        non_offline_failure = False
        offline_fallback_candidate: tuple[AIProvider, str, str] | None = None
        last_provider_id: str | None = None
        max_retries = self._max_retries()

        for provider in providers:
            last_provider_id = provider.id
            cred_ok, cred_err = validate_credential_ref(provider.credential_ref)
            if not cred_ok:
                provider.health_status = "degraded"
                provider.last_checked_at = utcnow()
                await db.commit()
                last_err = f"Provider {provider.id}: {cred_err}"
                non_offline_failure = True
                continue

            within_budget, budget_err = await self.check_spend_caps(db, provider)
            if not within_budget:
                last_err = budget_err or last_err
                budget_error = budget_err or budget_error
                budget_blocked = True
                non_offline_failure = True
                continue

            effective_model = self._select_model(provider, req.model)
            adapter = build_adapter(
                provider.provider_type,
                provider.id,
                provider.endpoint_url,
                provider.credential_ref,
                effective_model,
            )

            for attempt_index in range(max_retries + 1):
                attempt_req = replace(req, model=effective_model, metadata=dict(session_meta))
                response = await adapter.generate(attempt_req)
                for key in ("session_id", "account_id", "conversation_id"):
                    if key in session_meta and key not in response.metadata:
                        response.metadata[key] = session_meta[key]
                response.metadata.setdefault("attempt", attempt_index + 1)
                await self._record_usage_attempt(
                    db,
                    provider,
                    attempt_req,
                    response,
                    attempt=attempt_index + 1,
                )

                if response.success:
                    self._update_session_health(
                        employee_session,
                        success=True,
                        error_message=None,
                        transport_unavailable=False,
                    )
                    await db.commit()
                    return response

                last_err = response.error_message or last_err
                if response.retryable and attempt_index < max_retries:
                    delay = self._retry_delay(response)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue

                self._update_session_health(
                    employee_session,
                    success=False,
                    error_message=response.error_message,
                    transport_unavailable=bool(response.metadata.get("transport_unavailable")),
                )
                await db.commit()
                if response.metadata.get("bridge_offline"):
                    if offline_fallback_candidate is None:
                        offline_fallback_candidate = (provider, effective_model, last_err)
                else:
                    non_offline_failure = True
                break

        if req.allow_diagnostic_fallback and offline_fallback_candidate is not None and not non_offline_failure:
            offline_provider, offline_model, offline_reason = offline_fallback_candidate
            diagnostic = (
                f"[{offline_provider.id} | safe-diagnostic] Local bridge unavailable ({offline_reason}). "
                "Running in safe zero-spend diagnostic mode until the bridge daemon is started."
            )
            return ProviderResponse(
                provider_id=offline_provider.id,
                model=offline_model,
                content=diagnostic,
                input_tokens=BaseProviderAdapter.estimate_tokens(req.system_prompt + req.prompt),
                output_tokens=BaseProviderAdapter.estimate_tokens(diagnostic),
                estimated_cost_usd=0.0,
                latency_ms=0.0,
                success=True,
                metadata={
                    "mode": "bridge_offline_diagnostic",
                    "dry_run": True,
                    "bridge_offline": True,
                    "original_error": offline_reason,
                    **{
                        key: session_meta[key]
                        for key in ("session_id", "account_id", "conversation_id")
                        if key in session_meta
                    },
                },
                input_tokens_estimated=True,
                output_tokens_estimated=True,
            )

        return ProviderResponse(
            provider_id="none",
            model=req.model or "none",
            content="",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=0.0,
            success=False,
            error_message=budget_error or last_err,
            metadata={
                "budget_exceeded": budget_blocked,
                "last_provider_id": last_provider_id,
                "diagnostic_fallback_requested": req.allow_diagnostic_fallback,
            },
        )

    async def route_and_stream(
        self,
        db: AsyncSession,
        req: ProviderRequest,
        preferred_provider_id: str | None = None,
    ) -> AsyncIterator[ProviderStreamChunk]:
        """Route streaming requests with provider-agnostic retries and failover."""
        if req.dry_run:
            try:
                await self._resolve_employee_session_context(db, req)
            except ValueError as exc:
                yield ProviderStreamChunk(
                    provider_id=preferred_provider_id or "none",
                    model=req.model or "none",
                    done=True,
                    finish_reason="error",
                    success=False,
                    error_message=str(exc),
                    metadata={"cross_account_denied": True},
                )
                return
            metadata = {"dry_run": True, "mode": "dry_run", "stream": True}
            yield ProviderStreamChunk(
                provider_id=preferred_provider_id or "none",
                model=req.model or "none",
                delta="Dry run: no provider request was sent.",
                metadata=metadata,
            )
            yield ProviderStreamChunk(
                provider_id=preferred_provider_id or "none",
                model=req.model or "none",
                done=True,
                finish_reason="stop",
                success=True,
                metadata=metadata,
            )
            return

        res = await db.execute(
            select(AIProvider).where(AIProvider.is_enabled.is_(True)).order_by(AIProvider.priority.asc())
        )
        providers = list(res.scalars().all())
        if preferred_provider_id:
            providers.sort(key=lambda p: (0 if p.id == preferred_provider_id else 1, p.priority))

        try:
            session_meta, employee_session = await self._resolve_employee_session_context(db, req)
        except ValueError as exc:
            yield ProviderStreamChunk(
                provider_id=preferred_provider_id or "none",
                model=req.model or "none",
                done=True,
                finish_reason="error",
                success=False,
                error_message=str(exc),
                metadata={"cross_account_denied": True},
            )
            return

        last_err = "No enabled AI providers available."
        budget_blocked = False
        budget_error: str | None = None
        non_offline_failure = False
        offline_fallback_candidate: tuple[AIProvider, str, str] | None = None
        last_provider_id: str | None = None
        max_retries = self._max_retries()

        for provider in providers:
            last_provider_id = provider.id
            cred_ok, cred_err = validate_credential_ref(provider.credential_ref)
            if not cred_ok:
                provider.health_status = "degraded"
                provider.last_checked_at = utcnow()
                await db.commit()
                last_err = f"Provider {provider.id}: {cred_err}"
                non_offline_failure = True
                continue

            within_budget, budget_err = await self.check_spend_caps(db, provider)
            if not within_budget:
                last_err = budget_err or last_err
                budget_error = budget_err or budget_error
                budget_blocked = True
                non_offline_failure = True
                continue

            effective_model = self._select_model(provider, req.model)
            adapter = build_adapter(
                provider.provider_type,
                provider.id,
                provider.endpoint_url,
                provider.credential_ref,
                effective_model,
            )

            for attempt_index in range(max_retries + 1):
                attempt_req = replace(req, model=effective_model, metadata=dict(session_meta))
                emitted_any = False
                terminal_chunk: ProviderStreamChunk | None = None
                async for chunk in adapter.generate_stream(attempt_req):
                    for key in ("session_id", "account_id", "conversation_id"):
                        if key in session_meta and key not in chunk.metadata:
                            chunk.metadata[key] = session_meta[key]
                    if not chunk.done:
                        if chunk.delta:
                            emitted_any = True
                        yield chunk
                    else:
                        terminal_chunk = chunk

                if terminal_chunk is None:
                    terminal_chunk = ProviderStreamChunk(
                        provider_id=provider.id,
                        model=effective_model,
                        done=True,
                        finish_reason="error",
                        success=False,
                        error_message=f"Provider {provider.id} terminated stream without a completion frame.",
                        metadata={"stream": True, **session_meta},
                    )
                terminal_chunk.metadata.setdefault("attempt", attempt_index + 1)
                await self._record_usage_attempt(
                    db,
                    provider,
                    attempt_req,
                    terminal_chunk,
                    attempt=attempt_index + 1,
                    streaming=True,
                )

                if terminal_chunk.success:
                    self._update_session_health(
                        employee_session,
                        success=True,
                        error_message=None,
                        transport_unavailable=False,
                    )
                    await db.commit()
                    yield terminal_chunk
                    return

                last_err = terminal_chunk.error_message or last_err
                if terminal_chunk.retryable and not emitted_any and attempt_index < max_retries:
                    delay = self._retry_delay(terminal_chunk)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue

                self._update_session_health(
                    employee_session,
                    success=False,
                    error_message=terminal_chunk.error_message,
                    transport_unavailable=bool(terminal_chunk.metadata.get("transport_unavailable")),
                )
                await db.commit()
                if emitted_any:
                    yield terminal_chunk
                    return
                if terminal_chunk.metadata.get("bridge_offline"):
                    if offline_fallback_candidate is None:
                        offline_fallback_candidate = (provider, effective_model, last_err)
                else:
                    non_offline_failure = True
                break

        if req.allow_diagnostic_fallback and offline_fallback_candidate is not None and not non_offline_failure:
            offline_provider, offline_model, offline_reason = offline_fallback_candidate
            diagnostic = (
                f"[{offline_provider.id} | safe-diagnostic] Local bridge unavailable ({offline_reason}). "
                "Running in safe zero-spend diagnostic mode until the bridge daemon is started."
            )
            diagnostic_meta = {
                "mode": "bridge_offline_diagnostic",
                "dry_run": True,
                "bridge_offline": True,
                "stream": True,
                "original_error": offline_reason,
                **{
                    key: session_meta[key]
                    for key in ("session_id", "account_id", "conversation_id")
                    if key in session_meta
                },
            }
            yield ProviderStreamChunk(
                provider_id=offline_provider.id,
                model=offline_model,
                delta=diagnostic,
                done=False,
                metadata=diagnostic_meta,
            )
            yield ProviderStreamChunk(
                provider_id=offline_provider.id,
                model=offline_model,
                delta="",
                done=True,
                finish_reason="stop",
                input_tokens=BaseProviderAdapter.estimate_tokens(req.system_prompt + req.prompt),
                output_tokens=BaseProviderAdapter.estimate_tokens(diagnostic),
                estimated_cost_usd=0.0,
                success=True,
                metadata=diagnostic_meta,
                input_tokens_estimated=True,
                output_tokens_estimated=True,
            )
            return

        yield ProviderStreamChunk(
            provider_id="none",
            model=req.model or "none",
            delta="",
            done=True,
            finish_reason="error",
            success=False,
            error_message=budget_error or last_err,
            metadata={
                "budget_exceeded": budget_blocked,
                "last_provider_id": last_provider_id,
                "stream": True,
            },
        )

    async def recover_degraded_sessions(
        self,
        db: AsyncSession,
        *,
        health_threshold: float = 50.0,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Reset degraded ProviderSessions (health_score < threshold or status='degraded'), clearing bridge state cleanly."""
        res = await db.execute(
            select(ProviderSession).where(
                ProviderSession.status != "closed",
                or_(
                    ProviderSession.health_score < health_threshold,
                    ProviderSession.status == "degraded",
                ),
            )
        )
        degraded_sessions = list(res.scalars().all())
        recovered: list[dict[str, Any]] = []

        for sess in degraded_sessions:
            bridge_provider: AIProvider | None = None
            if sess.account_id:
                acct = await db.get(ProviderAccount, sess.account_id)
                if acct and acct.provider_id:
                    cand = await db.get(AIProvider, acct.provider_id)
                    if cand and cand.provider_type == "arena_local_bridge":
                        bridge_provider = cand

            if bridge_provider is None and sess.agent_id:
                ag = await db.get(Agent, sess.agent_id)
                if ag and ag.provider_id:
                    cand = await db.get(AIProvider, ag.provider_id)
                    if cand and cand.provider_type == "arena_local_bridge":
                        bridge_provider = cand

            bridge_reset: dict[str, Any] | None = None
            if bridge_provider is not None:
                adapter = ArenaLocalBridgeAdapter(
                    provider_id=bridge_provider.id,
                    endpoint_url=bridge_provider.endpoint_url,
                    credential_ref=bridge_provider.credential_ref,
                    default_model=bridge_provider.default_model,
                )
                try:
                    bridge_reset = await adapter.delete_session(sess.id, account_id=sess.account_id)
                except Exception as exc:  # noqa: BLE001 - recovery must not abort on offline bridge
                    bridge_reset = {
                        "deleted": False,
                        "session_id": sess.id,
                        "account_id": sess.account_id,
                        "status": "error",
                        "bridge_reachable": False,
                        "error": f"Bridge reset error: {type(exc).__name__}",
                    }

            prev_score = float(sess.health_score)
            prev_status = sess.status
            prev_errors = int(sess.error_count)

            sess.health_score = 100.0
            sess.status = "active"
            sess.error_count = 0
            sess.last_error = None

            entry = {
                "session_id": sess.id,
                "account_id": sess.account_id,
                "agent_id": sess.agent_id,
                "previous_health_score": prev_score,
                "previous_status": prev_status,
                "previous_error_count": prev_errors,
                "new_health_score": 100.0,
                "new_status": "active",
                "bridge_reset": bridge_reset,
            }
            recovered.append(entry)
            db.add(
                AuditLog(
                    actor=actor,
                    action="provider.session_recovered",
                    resource_type="provider_session",
                    resource_id=sess.id,
                    details=entry,
                )
            )

        if recovered:
            await db.commit()
            await event_bus.emit(
                "provider.sessions_recovered",
                {"recovered_count": len(recovered), "session_ids": [r["session_id"] for r in recovered]},
            )

        return {
            "recovered_count": len(recovered),
            "threshold": health_threshold,
            "sessions": recovered,
        }


async def get_model_configuration_status(db: AsyncSession) -> dict[str, Any]:
    """Inspect enabled AI providers and distinguish real configured models from diagnostic-only local bridge."""
    settings = get_settings()
    res = await db.execute(select(AIProvider).where(AIProvider.is_enabled.is_(True)).order_by(AIProvider.priority.asc()))
    providers = list(res.scalars().all())
    upstream_url = os.environ.get("ARENA_BRIDGE_UPSTREAM_URL", "").strip()
    default_bridge_urls = {
        "",
        "http://127.0.0.1:4100",
        "http://localhost:4100",
        (settings.ARENA_BRIDGE_URL or "").strip().rstrip("/"),
    }

    configured_providers: list[str] = []
    diagnostic_only_providers: list[str] = []
    for p in providers:
        if p.provider_type == "arena_agent":
            continue
        has_cred, _ = validate_credential_ref(p.credential_ref)
        if p.provider_type == "arena_local_bridge":
            ep = (p.endpoint_url or "").strip().rstrip("/")
            if has_cred and (upstream_url or (ep not in default_bridge_urls)):
                configured_providers.append(p.id)
            else:
                diagnostic_only_providers.append(p.id)
        elif has_cred:
            configured_providers.append(p.id)

    configured = len(configured_providers) > 0
    return {
        "status": "CONFIGURED" if configured else "NO_MODEL_CONFIGURED",
        "configured": configured,
        "configured_providers": configured_providers,
        "diagnostic_only_providers": diagnostic_only_providers,
        "reason": (
            None
            if configured
            else (
                "No real AI model provider is configured. Configure Claude API, an OpenAI-compatible/Ollama endpoint, "
                "or Arena Local Bridge with ARENA_BRIDGE_UPSTREAM_URL."
            )
        ),
    }


provider_router = ProviderRouter()
