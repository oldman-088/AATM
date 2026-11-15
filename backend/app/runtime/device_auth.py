"""Device-scoped authentication for remote worker daemons.

A remote execution node is not a human operator and does not hold an admin JWT.
Instead each device gets a token bound to its device id, derived from the server
secret with HMAC-SHA256:

    token = HMAC-SHA256(SECRET_KEY, device_id)

Properties:
* The token cannot be forged without the server secret.
* The token is bound to exactly one device id, so a stolen token cannot be replayed
  against a different node.
* Nothing is stored in the database, so a database leak does not expose device tokens.

Because the token is derived rather than persisted it cannot be revoked individually;
rotating `SECRET_KEY` revokes every device token at once. That trade-off is
documented in `docs/SECURITY_ASSESSMENT.md`.
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


def issue_device_token(device_id: str) -> str:
    """Derive the deterministic authentication token for a device id."""
    settings = get_settings()
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        device_id.encode("utf-8"),
        sha256,
    ).hexdigest()


def verify_device_token(device_id: str, token: str | None) -> bool:
    """Constant-time verification of a device token against its device id."""
    if not token or not device_id:
        return False
    return hmac.compare_digest(issue_device_token(device_id), token.strip())
