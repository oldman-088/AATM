"""Read-only media proxying for result avatars.

The dashboard runs under a strict ``img-src 'self' data: blob:`` CSP
(``app/main.py::security_headers_middleware``), so a social-media profile
picture that lives on a third-party CDN cannot be rendered directly. Instead of
weakening the CSP, the frontend points ``<img src>`` at
``GET /api/v1/media/avatar?url=<https url>`` and this route fetches the image
server-side and streams it back.

Safety controls (the URL is untrusted data coming from a provider payload):

* only ``http``/``https`` URLs with a hostname and no embedded credentials;
* the hostname is resolved first and every resolved address must be globally
  routable (no loopback / private / link-local / reserved targets) — the same
  policy ``app/browser/safety.py`` applies to navigation;
* the resolved IP that was validated is the IP that gets connected to (no
  second DNS lookup, so no DNS-rebinding window), and HTTP redirects are not
  followed;
* an allow-list of image content types and a hard byte cap on the body;
* the request is authenticated like every other platform route.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_current_user

router = APIRouter(tags=["media"], dependencies=[Depends(get_current_user)])

MAX_AVATAR_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 6.0
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
ALLOWED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"})


def _public_address(host: str, port: int) -> str:
    """Resolve ``host`` and return one validated public IPv4/IPv6 literal."""
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:  # pragma: no cover - depends on live DNS
        raise HTTPException(status_code=400, detail="avatar host could not be resolved") from exc
    if not records:
        raise HTTPException(status_code=400, detail="avatar host could not be resolved")
    for record in records:
        raw = record[4][0]
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:  # pragma: no cover - defensive
            continue
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global:
            raise HTTPException(status_code=400, detail="avatar URL must point to a public host")
        return str(address)
    raise HTTPException(status_code=400, detail="avatar host could not be resolved")


def validate_avatar_url(url: str) -> str:
    """Return ``url`` when it is a safe, public http(s) image URL; otherwise raise 400."""
    cleaned = (url or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="url is required")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="avatar URL must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(status_code=400, detail="avatar URL must not contain credentials")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise HTTPException(status_code=400, detail="avatar URL must point to a public host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return cleaned  # a DNS name: resolved and re-validated at fetch time
    if isinstance(literal, ipaddress.IPv6Address) and literal.ipv4_mapped is not None:
        literal = literal.ipv4_mapped
    if not literal.is_global:
        raise HTTPException(status_code=400, detail="avatar URL must point to a public host")
    return cleaned


@router.get("/media/avatar")
async def avatar_proxy(url: str = Query(min_length=1, max_length=2048)) -> Response:
    """Fetch one public profile image and stream it back under the app's own origin."""
    target = validate_avatar_url(url)
    parsed = urlparse(target)
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="avatar URL contains an invalid port") from exc

    # Pin the validated address so the connection cannot be re-pointed by DNS.
    pinned_host = _public_address(host, port)
    safe_url = parsed._replace(netloc=f"{pinned_host}:{port}").geturl()

    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False, verify=True
        ) as client:
            async with client.stream(
                "GET", safe_url, headers={"Host": host, "User-Agent": "AATM/1.0 (+avatar-proxy)"}
            ) as upstream:
                if upstream.status_code != 200:
                    raise HTTPException(status_code=502, detail="avatar could not be fetched")
                content_type = (upstream.headers.get("content-type") or "").split(";")[0].strip().lower()
                if content_type not in ALLOWED_IMAGE_TYPES:
                    raise HTTPException(status_code=502, detail="avatar response is not a supported image")
                body = bytearray()
                async for chunk in upstream.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_AVATAR_BYTES:
                        raise HTTPException(status_code=502, detail="avatar exceeds the maximum size")
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="avatar could not be fetched") from exc

    if not body:
        raise HTTPException(status_code=502, detail="avatar response was empty")
    return Response(
        content=bytes(body),
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"},
    )
