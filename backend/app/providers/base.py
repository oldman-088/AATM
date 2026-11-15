import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.security import resolve_credential_ref

ENV_CREDENTIAL_REF_PATTERN = re.compile(r"^env:[A-Z_][A-Z0-9_]*$")


def validate_credential_ref(credential_ref: str | None) -> tuple[bool, str]:
    """Enforce env:* indirection; reject raw inline secrets or non-env credential references."""
    if credential_ref is None or credential_ref == "":
        return True, "none"
    if ENV_CREDENTIAL_REF_PATTERN.fullmatch(credential_ref.strip()):
        return True, "env_ref"
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
        self.credential_ref_valid, self.credential_ref_error = validate_credential_ref(credential_ref)

    def resolve_credential(self) -> str | None:
        if not self.credential_ref_valid:
            return None
        return resolve_credential_ref(self.credential_ref)

    def safe_credential_label(self) -> str:
        if not self.credential_ref:
            return "unconfigured"
        if self.credential_ref_valid:
            return self.credential_ref.strip()
        return "<invalid-credential-ref>"

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        ...

    @abstractmethod
    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        ...

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)
