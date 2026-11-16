"""Provider credential + endpoint policy (secret-exfiltration guard).

Threat: ``credential_ref`` is an ``env:NAME`` indirection that the backend
resolves and sends as a bearer/API key to ``endpoint_url``.  Without a policy an
operator could point ``endpoint_url`` at a host they control and set
``credential_ref=env:SECRET_KEY`` (or ``DATABASE_URL``, ``GITHUB_TOKEN`` ...) to
exfiltrate server secrets through "Test connection".

Rules enforced here (API layer = full policy, runtime = deny-list):

1. ``credential_ref`` may only name a provider-key variable: ``^[A-Z][A-Z0-9_]*_API_KEY$``
   or a name in ``PROVIDER_CREDENTIAL_ENV_ALLOWLIST`` (comma separated setting).
2. Server secrets are *always* denied, even if allow-listed: everything in
   ``runtime.sandbox.SENSITIVE_ENV_EXACT`` that is not itself a provider key,
   plus GitHub tokens, webhook secrets and any ``AATM_*`` variable.
3. ``ARENA_BRIDGE_TOKEN`` is a *bound* credential: it may only be sent by an
   ``arena_local_bridge`` provider to a loopback host or to the configured
   ``ARENA_BRIDGE_URL`` origin.
4. ``endpoint_url`` must be ``http``/``https`` with a host, no embedded
   credentials (``user:pass@``), no fragment.
5. Operators may only use the official preset endpoint + credential pairs;
   any custom endpoint (or custom pairing) requires the ``admin`` role.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.core.config import get_settings
from app.runtime.sandbox import SENSITIVE_ENV_EXACT

PROVIDER_KEY_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")
_ENV_REF_PATTERN = re.compile(r"^env:([A-Z_][A-Z0-9_]*)$")

# Provider keys that also appear in the sandbox's SENSITIVE_ENV_EXACT (they must
# never reach agent subprocesses, but they ARE legitimate provider credentials).
_PROVIDER_KEYS_IN_SANDBOX_DENYLIST = frozenset({"ANTHROPIC_API_KEY"})

BOUND_CREDENTIAL_ENVS: dict[str, str] = {"ARENA_BRIDGE_TOKEN": "arena_local_bridge"}
# Provider types that may carry a bound credential (Phase 6: the Arena browser connector also
# talks only to the same local bridge).
BOUND_CREDENTIAL_PROVIDER_TYPES: dict[str, frozenset[str]] = {
    "ARENA_BRIDGE_TOKEN": frozenset({"arena_local_bridge", "arena_browser"}),
}

SERVER_SECRET_ENV_DENYLIST: frozenset[str] = frozenset(
    (set(SENSITIVE_ENV_EXACT) - _PROVIDER_KEYS_IN_SANDBOX_DENYLIST - set(BOUND_CREDENTIAL_ENVS))
    | {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
        "ARENA_SESSION_TOKEN",
        "REDIS_URL",
        "RABBITMQ_URL",
        "AMQP_URL",
        "POSTGRES_PASSWORD",
    }
)

CREDENTIAL_POLICY_ERROR = (
    "credential_ref must name a provider API-key variable (env:<NAME>_API_KEY) or an entry of "
    "PROVIDER_CREDENTIAL_ENV_ALLOWLIST; server secrets are never accepted."
)


class CredentialPolicyError(ValueError):
    """Raised when a credential_ref / endpoint_url combination violates policy."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "CREDENTIAL_POLICY_VIOLATION"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _allowlist() -> set[str]:
    raw = getattr(get_settings(), "PROVIDER_CREDENTIAL_ENV_ALLOWLIST", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def env_name_from_ref(credential_ref: str | None) -> str | None:
    if credential_ref is None:
        return None
    match = _ENV_REF_PATTERN.fullmatch(credential_ref.strip())
    return match.group(1) if match else None


def is_server_secret_env(name: str) -> bool:
    upper = name.strip().upper()
    if upper in SERVER_SECRET_ENV_DENYLIST:
        return True
    return upper.startswith("AATM_")


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower(), port or (443 if parts.scheme == "https" else 80)


def _is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_endpoint_url(endpoint_url: str | None) -> str | None:
    """Return the normalized endpoint or raise CredentialPolicyError."""
    if endpoint_url is None or not endpoint_url.strip():
        return None
    url = endpoint_url.strip()
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise CredentialPolicyError("endpoint_url is malformed.") from None
    if parts.scheme not in ("http", "https"):
        raise CredentialPolicyError("endpoint_url must use http or https.")
    if not parts.hostname:
        raise CredentialPolicyError("endpoint_url must include a host.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise CredentialPolicyError("endpoint_url must not embed credentials.")
    if parts.fragment:
        raise CredentialPolicyError("endpoint_url must not contain a fragment.")
    if any(ch.isspace() for ch in url):
        raise CredentialPolicyError("endpoint_url must not contain whitespace.")
    return url


def _bound_credential_allowed(env_name: str, provider_type: str | None, endpoint_url: str | None) -> bool:
    required_type = BOUND_CREDENTIAL_ENVS.get(env_name)
    if required_type is None:
        return False
    allowed_types = BOUND_CREDENTIAL_PROVIDER_TYPES.get(env_name, frozenset({required_type}))
    if provider_type is not None and provider_type not in allowed_types:
        return False
    settings = get_settings()
    target = _origin(endpoint_url or settings.ARENA_BRIDGE_URL)
    if target is None:
        return False
    if _is_loopback_host(target[1]):
        return True
    return target == _origin(settings.ARENA_BRIDGE_URL)


def validate_provider_credential_ref(
    credential_ref: str | None,
    *,
    provider_type: str | None,
    endpoint_url: str | None,
    required: bool = False,
) -> str | None:
    """Full API-layer policy for a credential_ref."""
    if credential_ref is None or not credential_ref.strip():
        if required:
            raise CredentialPolicyError(CREDENTIAL_POLICY_ERROR)
        return None
    env_name = env_name_from_ref(credential_ref)
    if env_name is None:
        raise CredentialPolicyError(
            "credential_ref must reference an environment variable (env:NAME); inline credentials are not accepted."
        )
    if env_name in BOUND_CREDENTIAL_ENVS:
        if _bound_credential_allowed(env_name, provider_type, endpoint_url):
            return credential_ref.strip()
        raise CredentialPolicyError(
            f"env:{env_name} may only be sent to the local Arena bridge (loopback or ARENA_BRIDGE_URL).",
        )
    if is_server_secret_env(env_name):
        raise CredentialPolicyError(CREDENTIAL_POLICY_ERROR)
    if not (PROVIDER_KEY_ENV_PATTERN.fullmatch(env_name) or env_name in _allowlist()):
        raise CredentialPolicyError(CREDENTIAL_POLICY_ERROR)
    return credential_ref.strip()


def runtime_credential_denial(
    credential_ref: str | None, *, provider_type: str | None, endpoint_url: str | None
) -> str | None:
    """Defense in depth for rows that bypassed the API (legacy DB rows, seeds).

    Returns an error message when the credential must not be resolved, else None.
    """
    env_name = env_name_from_ref(credential_ref)
    if env_name is None:
        return None
    if env_name in BOUND_CREDENTIAL_ENVS:
        if _bound_credential_allowed(env_name, provider_type, endpoint_url):
            return None
        return f"Credential env:{env_name} is bound to the local Arena bridge and cannot be sent to this endpoint."
    if is_server_secret_env(env_name):
        return f"Credential env:{env_name} is a server secret and is never sent to providers."
    return None


# ---------------------------------------------------------------------------
# Official presets (single source for the wizard + operator restrictions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PresetEndpoint:
    provider_type: str
    endpoint_url: str
    credential_ref: str


def official_preset_endpoints() -> list[PresetEndpoint]:
    settings = get_settings()
    anthropic_ref = settings.ANTHROPIC_API_KEY_REF or "env:ANTHROPIC_API_KEY"
    return [
        PresetEndpoint("anthropic_claude", "https://api.anthropic.com/v1", anthropic_ref),
        PresetEndpoint("openai_compatible", "http://127.0.0.1:11434/v1", "env:OLLAMA_API_KEY"),
        PresetEndpoint("openai_compatible", "https://openrouter.ai/api/v1", "env:OPENROUTER_API_KEY"),
        PresetEndpoint("openai_compatible", "http://127.0.0.1:4000/v1", "env:LITELLM_API_KEY"),
        PresetEndpoint("openai_compatible", "http://127.0.0.1:1234/v1", "env:OPENAI_API_KEY"),
        PresetEndpoint("arena_local_bridge", settings.ARENA_BRIDGE_URL, "env:ARENA_BRIDGE_TOKEN"),
    ]


def is_official_preset(provider_type: str, endpoint_url: str | None, credential_ref: str | None) -> bool:
    if endpoint_url is None or not endpoint_url.strip():
        # Only the Anthropic adapter has a built-in official default endpoint.
        endpoint_url = "https://api.anthropic.com/v1" if provider_type == "anthropic_claude" else None
        if endpoint_url is None:
            return False
    target = _origin(endpoint_url)
    if target is None:
        return False
    for preset in official_preset_endpoints():
        if preset.provider_type != provider_type:
            continue
        if _origin(preset.endpoint_url) != target:
            continue
        if credential_ref is None or credential_ref.strip() == preset.credential_ref:
            return True
    return False


def enforce_provider_config_policy(
    *,
    role: str,
    provider_type: str,
    endpoint_url: str | None,
    credential_ref: str | None,
    credential_required: bool = False,
) -> tuple[str | None, str | None]:
    """Validate a provider configuration for create/update/test. Returns (endpoint, credential_ref)."""
    endpoint = validate_endpoint_url(endpoint_url)
    ref = validate_provider_credential_ref(
        credential_ref, provider_type=provider_type, endpoint_url=endpoint, required=credential_required
    )
    if role != "admin" and not is_official_preset(provider_type, endpoint, ref):
        raise CredentialPolicyError(
            "Custom provider endpoints or credential pairings require the admin role; "
            "operators may only use the official presets.",
            status_code=403,
            code="CUSTOM_ENDPOINT_REQUIRES_ADMIN",
        )
    return endpoint, ref


def audit_details(provider_type: str, endpoint_url: str | None, credential_ref: str | None, **extra: Any) -> dict:
    """Audit payload that names the env reference but never contains the key value."""
    origin = _origin(endpoint_url) if endpoint_url else None
    return {
        "provider_type": provider_type,
        "endpoint_origin": f"{origin[0]}://{origin[1]}:{origin[2]}" if origin else None,
        "credential_ref": credential_ref,
        **extra,
    }
