"""Provider adapters, one module per provider type.

Every public and helper name that used to live in ``app/providers/adapters.py`` is re-exported
here so existing imports keep working.
"""

from app.providers.adapters.anthropic import AnthropicClaudeAdapter
from app.providers.adapters.arena_agent import ArenaAgentAdapter
from app.providers.adapters.arena_bridge import ArenaLocalBridgeAdapter
from app.providers.adapters.arena_browser import ArenaBrowserAdapter
from app.providers.adapters.common import (  # noqa: F401 - re-exported for existing imports
    _ARENA_BRIDGE_SEMAPHORE_LIMIT,
    _MODEL_PRICING_PATH,
    _PROFILE_SEMAPHORES,
    ARENA_BRIDGE_SEMAPHORE,
    DEFAULT_ANTHROPIC_URL,
    DEFAULT_ARENA_BRIDGE_URL,
    DEFAULT_BRIDGE_MAX_CONCURRENCY,
    DEFAULT_BRIDGE_TIMEOUT_SECONDS,
    MODEL_PRICING_CATALOG,
    SAFE_SESSION_ID_RE,
    _bridge_max_concurrency,
    _bridge_semaphore,
    _bridge_timeout_seconds,
    _configured_value,
    _extract_openai_completion,
    _load_model_pricing,
    _retry_after_seconds,
    _validate_http_endpoint,
    compute_cost,
    compute_cost_details,
)
from app.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from app.providers.adapters.unsupported import UnsupportedProviderAdapter
from app.providers.base import BaseProviderAdapter

__all__ = [
    "AnthropicClaudeAdapter",
    "ArenaAgentAdapter",
    "ArenaBrowserAdapter",
    "ArenaLocalBridgeAdapter",
    "OpenAICompatibleAdapter",
    "UnsupportedProviderAdapter",
    "build_adapter",
    "DEFAULT_ARENA_BRIDGE_URL",
    "DEFAULT_ANTHROPIC_URL",
    "DEFAULT_BRIDGE_TIMEOUT_SECONDS",
    "DEFAULT_BRIDGE_MAX_CONCURRENCY",
    "SAFE_SESSION_ID_RE",
    "_MODEL_PRICING_PATH",
    "_load_model_pricing",
    "MODEL_PRICING_CATALOG",
    "compute_cost_details",
    "compute_cost",
    "_configured_value",
    "_bridge_timeout_seconds",
    "_bridge_max_concurrency",
    "_ARENA_BRIDGE_SEMAPHORE_LIMIT",
    "ARENA_BRIDGE_SEMAPHORE",
    "_PROFILE_SEMAPHORES",
    "_bridge_semaphore",
    "_retry_after_seconds",
    "_extract_openai_completion",
    "_validate_http_endpoint",
]


def build_adapter(
    provider_type: str,
    provider_id: str,
    endpoint_url: str | None,
    credential_ref: str | None,
    default_model: str,
) -> BaseProviderAdapter:
    adapter_types: dict[str, type[BaseProviderAdapter]] = {
        "arena_local_bridge": ArenaLocalBridgeAdapter,
        "arena_agent": ArenaAgentAdapter,
        "arena_browser": ArenaBrowserAdapter,
        "anthropic_claude": AnthropicClaudeAdapter,
        "openai_compatible": OpenAICompatibleAdapter,
    }
    adapter_type = adapter_types.get(provider_type, UnsupportedProviderAdapter)
    return adapter_type(provider_id, endpoint_url, credential_ref, default_model)

