"""Explicit failure adapter for unknown provider types."""



from app.providers.base import (
    BaseProviderAdapter,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
)


class UnsupportedProviderAdapter(BaseProviderAdapter):
    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, "degraded", 0.0, "Provider adapter is not registered.", False)

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
            error_message="Provider adapter is not registered.",
            metadata={"unsupported_provider": True},
        )

