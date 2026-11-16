import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.core.config import get_settings

ALGORITHM = "HS256"

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*:\s*you\s+are\s+now", re.IGNORECASE),
    re.compile(r"bypass\s+(security|auth|captcha|guardrails)", re.IGNORECASE),
    re.compile(r"exfiltrate\s+(secrets|tokens|keys|passwords)", re.IGNORECASE),
]


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, salt, hex_digest = stored_hash.split("$", 2)
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
        return hmac.compare_digest(dk.hex(), hex_digest)
    except Exception:
        return False


def create_access_token(
    subject: str,
    role: str,
    token_version: int = 1,
    extra: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "token_version": int(token_version),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])


def resolve_credential_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    if ref.startswith("env:"):
        env_name = ref[4:].strip()
        return os.environ.get(env_name)
    return None


def mask_credential_ref(ref: str | None) -> str:
    if not ref:
        return "unconfigured"
    if ref.startswith("env:"):
        env_name = ref[4:].strip()
        present = bool(os.environ.get(env_name))
        return f"{ref} ({'configured' if present else 'not set'})"
    return "masked-ref"


def detect_prompt_injection(text: str) -> list[str]:
    hits: list[str] = []
    for pat in INJECTION_PATTERNS:
        if pat.search(text or ""):
            hits.append(pat.pattern)
    return hits


def sanitize_untrusted_content(text: str, source: str = "external") -> dict[str, Any]:
    """Isolate untrusted external content so it cannot be interpreted as trusted system instructions."""
    hits = detect_prompt_injection(text)
    cleaned = text.replace("```system", "```untrusted").replace("<system>", "[untrusted-tag]")
    return {
        "source": source,
        "injection_detected": bool(hits),
        "matched_patterns": hits,
        "quarantined_text": f"[UNTRUSTED_DATA source={source}]\n{cleaned}\n[/UNTRUSTED_DATA]",
    }


_SENSITIVE_KEY_RE = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|credential|authorization|cookie|private[_-]?key)",
    re.IGNORECASE,
)
_INLINE_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE)


def redact_sensitive_mapping(value: Any) -> Any:
    """Recursively redact sensitive keys and inline bearer credentials from audit/approval payloads."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if _SENSITIVE_KEY_RE.search(key_str) and not key_str.endswith("_status") and key_str != "token_version":
                out[key_str] = "[REDACTED]"
            else:
                out[key_str] = redact_sensitive_mapping(v)
        return out
    if isinstance(value, list):
        return [redact_sensitive_mapping(item) for item in value]
    if isinstance(value, str):
        return _INLINE_BEARER_RE.sub(r"\1[REDACTED]", value)
    return value
