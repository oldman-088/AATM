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
        bridge_offline: bool,
        is_dry_or_diag: bool,
    ) -> None:
        if employee_session is None or is_dry_or_diag:
            return
        if success:
            employee_session.message_count += 1
            employee_session.last_message_at = utcnow()
            employee_session.last_error = None
            employee_session.health_score = min(100.0, round(float(employee_session.health_score) + 10.0, 2))
            if employee_session.status == "degraded" and employee_session.health_score >= 60.0:
                employee_session.status = "active"
        else:
            penalty = 5.0 if bridge_offline else 15.0
            employee_session.error_count += 1
            employee_session.last_error = error_message
            employee_session.health_score = max(0.0, round(float(employee_session.health_score) - penalty, 2))
            if employee_session.health_score < 50.0 and employee_session.status == "active":
                employee_session.status = "degraded"

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
        non_offline_failure = False
        offline_fallback_candidate: tuple[AIProvider, str, str] | None = None

        for p in providers:
            cred_ok, cred_err = validate_credential_ref(p.credential_ref)
            if not cred_ok:
                p.health_status = "degraded"
                p.last_checked_at = utcnow()
                await db.commit()
                last_err = f"Provider {p.id}: {cred_err}"
                non_offline_failure = True
                continue

            if not req.dry_run:
                within_budget, budget_err = await self.check_spend_caps(db, p)
                if not within_budget:
                    last_err = budget_err or last_err
                    budget_blocked = True
                    non_offline_failure = True
                    continue

            effective_model = self._select_model(p, req.model)
            attempt_req = replace(req, model=effective_model, metadata=dict(session_meta))
            adapter = build_adapter(
                p.provider_type,
                p.id,
                p.endpoint_url,
                p.credential_ref,
                effective_model,
            )
            response = await adapter.generate(attempt_req)

            # Ensure response metadata preserves resolved employee session context
            for key in ("session_id", "account_id", "conversation_id"):
                if key in session_meta and key not in response.metadata:
                    response.metadata[key] = session_meta[key]

            is_dry_or_diag = bool(req.dry_run or response.metadata.get("dry_run"))
            recorded_cost = 0.0 if is_dry_or_diag else response.estimated_cost_usd
            confirmed_cost = (
                recorded_cost
                if (response.success and response.metadata.get("confirmed_usage") and not is_dry_or_diag)
                else None
            )

            db.add(
                ProviderUsageLog(
                    provider_id=p.id,
                    agent_id=req.agent_id,
                    task_id=req.task_id,
                    model_name=response.model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    estimated_cost_usd=recorded_cost,
                    confirmed_cost_usd=confirmed_cost,
                    latency_ms=response.latency_ms,
                    success=response.success,
                    error_message=response.error_message,
                )
            )
            if response.metadata.get("bridge_offline"):
                p.health_status = "offline"
            else:
                p.health_status = "healthy" if response.success else "degraded"
            p.latency_ms = response.latency_ms
            p.last_checked_at = utcnow()

            self._update_session_health(
                employee_session,
                success=response.success,
                error_message=response.error_message,
                bridge_offline=bool(response.metadata.get("bridge_offline")),
                is_dry_or_diag=is_dry_or_diag,
            )

            await db.commit()

            await event_bus.emit(
                "provider.usage",
                {
                    "provider_id": p.id,
                    "model": response.model,
                    "success": response.success,
                    "cost_usd": recorded_cost,
                    "session_id": session_meta.get("session_id"),
                    "account_id": session_meta.get("account_id"),
                },
            )
            if response.success:
                return response

            last_err = response.error_message or last_err
            if response.metadata.get("bridge_offline"):
                if offline_fallback_candidate is None:
                    offline_fallback_candidate = (p, effective_model, last_err)
            else:
                non_offline_failure = True

        if req.allow_diagnostic_fallback and offline_fallback_candidate is not None and not non_offline_failure:
            offline_provider, offline_model, offline_reason = offline_fallback_candidate
            in_tok = BaseProviderAdapter.estimate_tokens(req.system_prompt + req.prompt)
            diag_content = (
                f"[{offline_provider.id} | safe-diagnostic] Local bridge unavailable ({offline_reason}). "
                "Running in safe zero-spend diagnostic mode until the bridge daemon is started."
            )
            return ProviderResponse(
                provider_id=offline_provider.id,
                model=offline_model,
                content=diag_content,
                input_tokens=in_tok,
                output_tokens=BaseProviderAdapter.estimate_tokens(diag_content),
                estimated_cost_usd=0.0,
                latency_ms=0.0,
                success=True,
                metadata={
                    "mode": "bridge_offline_diagnostic",
                    "dry_run": True,
                    "bridge_offline": True,
                    "original_error": offline_reason,
                    **{k: session_meta[k] for k in ("session_id", "account_id", "conversation_id") if k in session_meta},
                },
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
            error_message=last_err,
            metadata={"budget_exceeded": budget_blocked},
        )

    async def route_and_stream(
        self,
        db: AsyncSession,
        req: ProviderRequest,
        preferred_provider_id: str | None = None,
    ) -> AsyncIterator[ProviderStreamChunk]:
        """Route streaming completion requests across providers with spend caps, cross-account isolation & failover."""
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
                delta="",
                done=True,
                finish_reason="error",
                success=False,
                error_message=str(exc),
                metadata={"cross_account_denied": True},
            )
            return

        last_err = "No enabled AI providers available."
        budget_blocked = False
        non_offline_failure = False
        offline_fallback_candidate: tuple[AIProvider, str, str] | None = None

        for p in providers:
            cred_ok, cred_err = validate_credential_ref(p.credential_ref)
            if not cred_ok:
                p.health_status = "degraded"
                p.last_checked_at = utcnow()
                await db.commit()
                last_err = f"Provider {p.id}: {cred_err}"
                non_offline_failure = True
                continue

            if not req.dry_run:
                within_budget, budget_err = await self.check_spend_caps(db, p)
                if not within_budget:
                    last_err = budget_err or last_err
                    budget_blocked = True
                    non_offline_failure = True
                    continue

            effective_model = self._select_model(p, req.model)
            attempt_req = replace(req, model=effective_model, metadata=dict(session_meta))
            adapter = build_adapter(
                p.provider_type,
                p.id,
                p.endpoint_url,
                p.credential_ref,
                effective_model,
            )

            emitted_any = False
            terminal_chunk: ProviderStreamChunk | None = None
            async for chunk in adapter.generate_stream(attempt_req):
                for key in ("session_id", "account_id", "conversation_id"):
                    if key in session_meta and key not in chunk.metadata:
                        chunk.metadata[key] = session_meta[key]
                if not chunk.done:
                    emitted_any = True
                    yield chunk
                else:
                    terminal_chunk = chunk

            if terminal_chunk is None:
                terminal_chunk = ProviderStreamChunk(
                    provider_id=p.id,
                    model=effective_model,
                    done=True,
                    finish_reason="error",
                    success=False,
                    error_message=f"Provider {p.id} terminated stream without completion frame.",
                    metadata={"stream": True, **session_meta},
                )

            is_dry_or_diag = bool(req.dry_run or terminal_chunk.metadata.get("dry_run"))
            recorded_cost = 0.0 if is_dry_or_diag else terminal_chunk.estimated_cost_usd
            confirmed_cost = (
                recorded_cost
                if (terminal_chunk.success and terminal_chunk.metadata.get("confirmed_usage") and not is_dry_or_diag)
                else None
            )

            db.add(
                ProviderUsageLog(
                    provider_id=p.id,
                    agent_id=req.agent_id,
                    task_id=req.task_id,
                    model_name=terminal_chunk.model,
                    input_tokens=terminal_chunk.input_tokens,
                    output_tokens=terminal_chunk.output_tokens,
                    estimated_cost_usd=recorded_cost,
                    confirmed_cost_usd=confirmed_cost,
                    latency_ms=terminal_chunk.latency_ms,
                    success=terminal_chunk.success,
                    error_message=terminal_chunk.error_message,
                )
            )
            if terminal_chunk.metadata.get("bridge_offline"):
                p.health_status = "offline"
            else:
                p.health_status = "healthy" if terminal_chunk.success else "degraded"
            p.latency_ms = terminal_chunk.latency_ms
            p.last_checked_at = utcnow()

            self._update_session_health(
                employee_session,
                success=terminal_chunk.success,
                error_message=terminal_chunk.error_message,
                bridge_offline=bool(terminal_chunk.metadata.get("bridge_offline")),
                is_dry_or_diag=is_dry_or_diag,
            )
            await db.commit()

            await event_bus.emit(
                "provider.usage",
                {
                    "provider_id": p.id,
                    "model": terminal_chunk.model,
                    "success": terminal_chunk.success,
                    "cost_usd": recorded_cost,
                    "stream": True,
                    "session_id": session_meta.get("session_id"),
                    "account_id": session_meta.get("account_id"),
                },
            )

            if terminal_chunk.success or emitted_any:
                yield terminal_chunk
                return

            last_err = terminal_chunk.error_message or last_err
            if terminal_chunk.metadata.get("bridge_offline"):
                if offline_fallback_candidate is None:
                    offline_fallback_candidate = (p, effective_model, last_err)
            else:
                non_offline_failure = True

        if req.allow_diagnostic_fallback and offline_fallback_candidate is not None and not non_offline_failure:
            offline_provider, offline_model, offline_reason = offline_fallback_candidate
            in_tok = BaseProviderAdapter.estimate_tokens(req.system_prompt + req.prompt)
            diag_content = (
                f"[{offline_provider.id} | safe-diagnostic] Local bridge unavailable ({offline_reason}). "
                "Running in safe zero-spend diagnostic mode until the bridge daemon is started."
            )
            diag_meta = {
                "mode": "bridge_offline_diagnostic",
                "dry_run": True,
                "stream": True,
                "bridge_offline": True,
                "original_error": offline_reason,
                **{k: session_meta[k] for k in ("session_id", "account_id", "conversation_id") if k in session_meta},
            }
            yield ProviderStreamChunk(
                provider_id=offline_provider.id,
                model=offline_model,
                delta=diag_content,
                done=False,
                metadata=diag_meta,
            )
            yield ProviderStreamChunk(
                provider_id=offline_provider.id,
                model=offline_model,
                delta="",
                done=True,
                finish_reason="stop",
                input_tokens=in_tok,
                output_tokens=BaseProviderAdapter.estimate_tokens(diag_content),
                estimated_cost_usd=0.0,
                latency_ms=0.0,
                success=True,
                metadata=diag_meta,
            )
            return

        yield ProviderStreamChunk(
            provider_id="none",
            model=req.model or "none",
            delta="",
            done=True,
            finish_reason="error",
            success=False,
            error_message=last_err,
            metadata={"budget_exceeded": budget_blocked, "stream": True},
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


provider_router = ProviderRouter()
