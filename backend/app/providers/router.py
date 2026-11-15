from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import event_bus
from app.db.base import utcnow
from app.models import AIProvider, ProviderUsageLog
from app.providers.adapters import build_adapter
from app.providers.base import ProviderHealth, ProviderRequest, ProviderResponse


class ProviderRouter:
    """Routes AI requests across enabled providers with budget enforcement, failover & usage logging."""

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

    async def get_daily_spend(self, db: AsyncSession, provider_id: str) -> float:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        res = await db.execute(
            select(func.coalesce(func.sum(ProviderUsageLog.estimated_cost_usd), 0.0)).where(
                ProviderUsageLog.provider_id == provider_id,
                ProviderUsageLog.created_at >= since,
            )
        )
        return float(res.scalar_one() or 0.0)

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
            providers.sort(key=lambda p: 0 if p.id == preferred_provider_id else p.priority)

        last_err = "No enabled AI providers available."
        for p in providers:
            daily_spend = await self.get_daily_spend(db, p.id)
            if daily_spend >= p.daily_limit_usd and not req.dry_run:
                last_err = f"Provider {p.id} reached daily budget cap (${p.daily_limit_usd:.2f})."
                continue

            adapter = build_adapter(
                p.provider_type,
                p.id,
                p.endpoint_url,
                p.credential_ref,
                req.model or p.default_model,
            )
            response = await adapter.generate(req)
            db.add(
                ProviderUsageLog(
                    provider_id=p.id,
                    agent_id=req.agent_id,
                    task_id=req.task_id,
                    model_name=response.model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    estimated_cost_usd=response.estimated_cost_usd,
                    latency_ms=response.latency_ms,
                    success=response.success,
                    error_message=response.error_message,
                )
            )
            p.health_status = "healthy" if response.success else "degraded"
            p.latency_ms = response.latency_ms
            p.last_checked_at = utcnow()
            await db.commit()

            await event_bus.emit(
                "provider.usage",
                {
                    "provider_id": p.id,
                    "model": response.model,
                    "success": response.success,
                    "cost_usd": response.estimated_cost_usd,
                },
            )
            if response.success:
                return response
            last_err = response.error_message or last_err

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
        )


provider_router = ProviderRouter()
