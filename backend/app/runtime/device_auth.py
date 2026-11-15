"""Device-scoped authentication for remote worker daemons.

A remote execution node is not a human operator and does not hold an admin JWT.
Instead, each device gets a token bound to its device id and database-backed token
version using HMAC-SHA256. Incrementing one device's version immediately invalidates
its prior tokens without rotating the server secret or affecting other devices.
"""

import hmac
from hashlib import sha256

from app.core.config import get_settings

# The development fallback secret from `app.core.config`. Using it in production would
# make every device token forgeable, so it is rejected explicitly.
DEFAULT_DEV_SECRET = "aatm-dev-secret-change-in-production-32bytes-minimum"


def using_default_secret() -> bool:
    """True when the server still runs with the well-known development secret."""
    return hmac.compare_digest(get_settings().SECRET_KEY, DEFAULT_DEV_SECRET)


def issue_device_token(device_id: str, token_version: int = 1) -> str:
    """Derive a token bound to both the device id and its current revocation version."""
    if not device_id or isinstance(token_version, bool) or not isinstance(token_version, int) or token_version < 1:
        raise ValueError("device_id and a positive token_version are required")

    settings = get_settings()
    message = f"{device_id}:v{token_version}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), message, sha256).hexdigest()


def verify_device_token(device_id: str, token: str | None, token_version: int = 1) -> bool:
    """Constant-time verification against the device id and currently stored version."""
    if not isinstance(token, str) or not token.strip() or not device_id:
        return False
    try:
        expected = issue_device_token(device_id, token_version)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, token.strip())
