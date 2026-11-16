"""arena_agent stub: explicitly not implemented."""

import time

from app.providers.base import (
    BaseProviderAdapter,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
)


class ArenaAgentAdapter(BaseProviderAdapter):
    provider_type = "arena_agent"

    async def health_check(self) -> ProviderHealth:
        t0 = time.perf_counter()
        return ProviderHealth(
            provider_id=self.provider_id,
            status="degraded",
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail="arena_agent runtime is not implemented",
            credentials_configured=bool(self.resolve_credential()),
        )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            model=req.model or self.default_model,
            content="",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=0.0,
            success=False,
            error_message="arena_agent runtime is not implemented",
            metadata={"runtime": "arena_agent", "implemented": False},
        )

