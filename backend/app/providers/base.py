from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderRequest:
    prompt: str
    system_prompt: str = ""
    model: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.2
    agent_id: str | None = None
    task_id: str | None = None
    dry_run: bool = False


@dataclass
class ProviderResponse:
    provider_id: str
    model: str
    content: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: float
    success: bool = True
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderHealth:
    provider_id: str
    status: str  # healthy | degraded | offline
    latency_ms: float
    detail: str
    credentials_configured: bool = False


class BaseProviderAdapter(ABC):
    def __init__(self, provider_id: str, endpoint_url: str | None, credential_ref: str | None, default_model: str):
        self.provider_id = provider_id
        self.endpoint_url = endpoint_url
        self.credential_ref = credential_ref
        self.default_model = default_model

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        ...

    @abstractmethod
    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        ...

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)
