import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.core.security import resolve_credential_ref

ENV_CREDENTIAL_REF_PATTERN = re.compile(r"^env:[A-Z_][A-Z0-9_]*$")


def validate_credential_ref(credential_ref: str | None) -> tuple[bool, str]:
    """Require a valid env reference whose value is present and non-empty at runtime."""
    if credential_ref is None or not credential_ref.strip():
        return False, "Credential is not configured."
    normalized = credential_ref.strip()
    if ENV_CREDENTIAL_REF_PATTERN.fullmatch(normalized):
        resolved = resolve_credential_ref(normalized)
        if resolved and resolved.strip():
            return True, "env_ref"
        return False, f"Credential {normalized} is not configured in the environment."
    return (
        False,
        "Invalid credential_ref: must use 'env:<ENV_VAR_NAME>' indirection (inline secrets are prohibited).",
    )


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
    allow_diagnostic_fallback: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


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
    retryable: bool = False
    retry_after_s: float | None = None
    input_tokens_estimated: bool = False
    output_tokens_estimated: bool = False
    confirmed_cost_usd: float | None = None
    pricing_known: bool = False


@dataclass
class ProviderStreamChunk:
    provider_id: str
    model: str
    delta: str = ""
    done: bool = False
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: float = 0.0
    success: bool = True
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    retry_after_s: float | None = None
    input_tokens_estimated: bool = False
    output_tokens_estimated: bool = False
    confirmed_cost_usd: float | None = None
    pricing_known: bool = False


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
        self._credential_ref_valid, self._credential_ref_error = validate_credential_ref(credential_ref)

    @property
    def credential_ref_valid(self) -> bool:
        """Re-check credentials at use time so runtime environment changes are respected."""
        self._credential_ref_valid, self._credential_ref_error = validate_credential_ref(self.credential_ref)
        return self._credential_ref_valid

    @property
    def credential_ref_error(self) -> str:
        self.credential_ref_valid
        return self._credential_ref_error

    def resolve_credential(self) -> str | None:
        if not self.credential_ref_valid:
            return None
        return resolve_credential_ref(self.credential_ref)

    def safe_credential_label(self) -> str:
        if not self.credential_ref or not self.credential_ref.strip():
            return "unconfigured"
        normalized = self.credential_ref.strip()
        if ENV_CREDENTIAL_REF_PATTERN.fullmatch(normalized):
            return normalized
        return "<invalid-credential-ref>"

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        ...

    @abstractmethod
    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        ...

    async def generate_stream(self, req: ProviderRequest) -> AsyncIterator[ProviderStreamChunk]:
        """Default streaming fallback wrapping generate() for adapters without native SSE streaming."""
        resp = await self.generate(req)
        if not resp.success:
            yield ProviderStreamChunk(
                provider_id=resp.provider_id,
                model=resp.model,
                delta="",
                done=True,
                finish_reason="error",
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                estimated_cost_usd=resp.estimated_cost_usd,
                latency_ms=resp.latency_ms,
                success=False,
                error_message=resp.error_message,
                metadata=dict(resp.metadata),
                retryable=resp.retryable,
                retry_after_s=resp.retry_after_s,
                input_tokens_estimated=resp.input_tokens_estimated,
                output_tokens_estimated=resp.output_tokens_estimated,
                confirmed_cost_usd=resp.confirmed_cost_usd,
            )
            return
        if resp.content:
            yield ProviderStreamChunk(
                provider_id=resp.provider_id,
                model=resp.model,
                delta=resp.content,
                done=False,
                metadata=dict(resp.metadata),
            )
        yield ProviderStreamChunk(
            provider_id=resp.provider_id,
            model=resp.model,
            delta="",
            done=True,
            finish_reason="stop",
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            estimated_cost_usd=resp.estimated_cost_usd,
            latency_ms=resp.latency_ms,
            success=True,
            metadata=dict(resp.metadata),
            input_tokens_estimated=resp.input_tokens_estimated,
            output_tokens_estimated=resp.output_tokens_estimated,
            confirmed_cost_usd=resp.confirmed_cost_usd,
        )

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)
