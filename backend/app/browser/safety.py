"""Browser action safety: selector/URL validation, SSRF guard and text redaction."""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse

MAX_SELECTOR_LENGTH = 512



MAX_FILL_VALUE_LENGTH = 50_000



MAX_EXTRACT_CHARS = 20_000



PROHIBITED_SELECTOR_RE = re.compile(
    r"(captcha|recaptcha|hcaptcha|turnstile|arkose|funcaptcha|geetest|cf-turnstile|g-recaptcha|h-captcha|px-captcha|datadome|challenge-form|javascript:|<script)",
    re.IGNORECASE,
)



SENSITIVE_TEXT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), "Bearer <redacted>"),
    (re.compile(r"\bAuthorization\s*:\s*[^\s,;\"']+(?:\s+[^\s,;\"']+)?", re.IGNORECASE), "Authorization: <redacted>"),
    (
        re.compile(
            r"\b((?:cookie|set-cookie|sessionid|session_id|csrftoken|cf_clearance|access_token|refresh_token|id_token|api_key|apikey|jwt|auth_token|password|passwd|secret)\s*[=:]\s*)([^\s;,&\"']+)",
            re.IGNORECASE,
        ),
        r"\1<redacted>",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "<redacted>"),
    (re.compile(r"\b(?:sk-(?:ant-|proj-)?|ghp_|gho_|github_pat_|xox[baprs]-|AIza|AKIA)[A-Za-z0-9_-]{12,}\b"), "<redacted>"),
]



def redact_extracted_text(text: str) -> tuple[str, int]:
    """Mask sensitive token, cookie, JWT, and credential patterns in extracted DOM text."""
    if not text:
        return "", 0
    redacted = text
    total_hits = 0
    for pattern, replacement in SENSITIVE_TEXT_PATTERNS:
        redacted, count = pattern.subn(replacement, redacted)
        total_hits += count
    return redacted, total_hits



def validate_selector(selector: str) -> str:
    """Validate DOM selectors for safe page interaction; block CAPTCHA/anti-bot challenge targets."""
    if not isinstance(selector, str):
        raise ValueError("Selector must be a non-empty string.")
    cleaned = selector.strip()
    if not cleaned:
        raise ValueError("Selector cannot be empty.")
    if len(cleaned) > MAX_SELECTOR_LENGTH:
        raise ValueError(f"Selector exceeds maximum allowed length ({MAX_SELECTOR_LENGTH} chars).")
    if PROHIBITED_SELECTOR_RE.search(cleaned):
        raise ValueError(
            "Policy violation: selector targets a prohibited CAPTCHA/anti-bot challenge or script pattern."
        )
    return cleaned



def validate_navigation_url(url: str) -> str:
    """Allow only safe web navigation schemes (http, https, about:blank)."""
    cleaned = (url or "").strip()
    if not cleaned or cleaned == "about:blank":
        return "about:blank"
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"Blocked unsafe navigation URL scheme '{parsed.scheme or 'none'}': only http://, https://, and about:blank are allowed."
        )
    return cleaned



SSRF_DNS_TIMEOUT_SECONDS = 3.0



BLOCKED_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal")



def _require_public_ip(raw_address: str) -> None:
    try:
        address = ipaddress.ip_address(raw_address.split("%", 1)[0])
    except ValueError as exc:
        raise ValueError("Navigation target resolved to an invalid IP address.") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_global:
        raise ValueError("Navigation to loopback, private, link-local, or reserved IP addresses is blocked.")



async def validate_public_navigation_url(url: str) -> str:
    """Reject credentials and non-public destinations, including DNS names resolving to private IPs."""
    safe_url = validate_navigation_url(url)
    if safe_url == "about:blank":
        return safe_url
    parsed = urlparse(safe_url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("Navigation URLs must have a hostname and must not contain embedded credentials.")
    if host == "localhost" or host.endswith(BLOCKED_LOCAL_HOST_SUFFIXES):
        raise ValueError("Navigation to local or internal hostnames is blocked.")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValueError("Navigation URL contains an invalid port.") from exc

    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        _require_public_ip(str(literal_address))
        return safe_url

    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM),
            timeout=SSRF_DNS_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutError) as exc:
        raise ValueError("Navigation hostname could not be resolved safely.") from exc
    addresses = {str(record[4][0]) for record in records if record and len(record) > 4 and record[4]}
    if not addresses:
        raise ValueError("Navigation hostname did not resolve to an address.")
    for address in addresses:
        _require_public_ip(address)
    return safe_url

