"""Arena browser connector hub (Phase 6): Tampermonkey userscript <-> local bridge.

* ``WS /v1/connector`` - one connection per employee browser profile. The userscript must send a
  ``hello`` with the per-profile token that AATM generated (only its SHA-256 is registered here)
  from an origin that AATM allowed for that profile.
* ``PUT/DELETE /v1/connector/profiles/{id}``, ``POST .../reset``, ``GET /v1/connector/profiles`` -
  backend-only administration, always protected by ``ARENA_BRIDGE_TOKEN``.
* ``dispatch()`` is used by ``/v1/chat/completions`` when ``model == "arena_browser"``.

Enforced here (and independently in the backend):
* one in-flight request per profile, a minimum delay between requests and a daily cap;
* a profile that reports ``needs_human`` (human check, verification page, rate-limit or terms
  prompt) stops immediately and stays stopped until a human resets it - nothing is solved or bypassed;
* real (non-loopback) Arena origins are accepted only when the backend registered the profile
  with ``real_enabled`` AND this bridge process itself has ``ARENA_CONNECTOR_ENABLED=true``,
  ``ARENA_AUTOMATION_APPROVED=true`` and a non-empty ``ARENA_APPROVAL_REFERENCE``;
* prompts, answers and tokens are never logged (only ids, lengths and statuses).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

logger = logging.getLogger("aatm.bridge.connector")

HELLO_TIMEOUT_SECONDS = 10.0
MAX_ANSWER_CHARS = 200_000
MAX_PROMPT_CHARS = 120_000

# Sent to the userscript as configuration data. The userscript never solves anything: when one
# of these appears it reports ``needs_human`` and stops.
DEFAULT_STOP_SELECTORS = [
    "iframe[src*='captcha']",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='challenge' i]",
    "[data-aatm-human-check]",
    "#challenge-form",
    "[class*='captcha']",
    "[id*='captcha']",
]
DEFAULT_STOP_PHRASES = [
    "verify you are human",
    "are you a robot",
    "unusual traffic",
    "rate limit",
    "too many requests",
    "terms of service",
    "terms of use",
    "accept the terms",
]
DEFAULT_DOM = {
    "input": "textarea",
    "send": "button[type='submit']",
    "assistant_message": "[data-role='assistant']",
    "user_message": "[data-role='user']",
    "busy": "[data-generating='true']",
}


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def real_arena_allowed_by_bridge() -> bool:
    return (
        _truthy("ARENA_CONNECTOR_ENABLED")
        and _truthy("ARENA_AUTOMATION_APPROVED")
        and bool(os.environ.get("ARENA_APPROVAL_REFERENCE", "").strip())
    )


def _origin_of(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"


def _is_loopback_origin(origin: str) -> bool:
    host = urlparse(origin).hostname or ""
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class ConnectorError(Exception):
    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass
class ProfileState:
    profile_id: str
    agent_id: str | None
    token_hash: str
    allowed_origins: list[str]
    real_enabled: bool
    min_interval_seconds: float
    daily_cap: int
    dom: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DOM))
    stop_selectors: list[str] = field(default_factory=lambda: list(DEFAULT_STOP_SELECTORS))
    stop_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_STOP_PHRASES))
    status: str = "offline"  # offline | ready | busy | needs_human
    needs_human_reason: str | None = None
    origin: str | None = None
    script_version: str | None = None
    last_seen: float | None = None
    last_request_end: float = 0.0
    day: str = field(default_factory=_utc_day)
    count_today: int = 0
    requests_total: int = 0
    ws: WebSocket | None = None
    connection_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)

    def health(self) -> str:
        if self.status == "needs_human":
            return "red"
        if self.status == "offline":
            return "red" if self.ws is None and self.last_seen else "yellow"
        if self.count_today >= self.daily_cap:
            return "yellow"
        return "green"

    def summary(self) -> dict[str, Any]:
        self._roll_day()
        return {
            "profile_id": self.profile_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "health": self.health(),
            "connected": self.ws is not None,
            "needs_human_reason": self.needs_human_reason,
            "origin": self.origin,
            "script_version": self.script_version,
            "last_seen_seconds_ago": round(time.monotonic() - self.last_seen, 1) if self.last_seen else None,
            "requests_today": self.count_today,
            "daily_cap": self.daily_cap,
            "min_interval_seconds": self.min_interval_seconds,
            "in_flight": self.lock.locked(),
            "real_enabled": self.real_enabled,
            "requests_total": self.requests_total,
        }

    def _roll_day(self) -> None:
        today = _utc_day()
        if today != self.day:
            self.day, self.count_today = today, 0


class ConnectorHub:
    def __init__(self) -> None:
        self.profiles: dict[str, ProfileState] = {}

    # ------------------------------------------------------------------ registration
    def register(self, profile_id: str, body: ProfileRegistration) -> ProfileState:
        origins: list[str] = []
        for raw in body.allowed_origins:
            origin = _origin_of(raw)
            if origin is None:
                raise ConnectorError("invalid_origin", "Allowed origins must be http(s) origins.", 422)
            if not _is_loopback_origin(origin):
                if not body.real_enabled:
                    raise ConnectorError("not_enabled", "Real Arena origins need the Arena approval flags.", 403)
                if not real_arena_allowed_by_bridge():
                    raise ConnectorError(
                        "not_enabled",
                        "This bridge process does not have ARENA_CONNECTOR_ENABLED, ARENA_AUTOMATION_APPROVED "
                        "and ARENA_APPROVAL_REFERENCE set; real Arena stays off.",
                        403,
                    )
            origins.append(origin)
        if len(body.token_sha256) != 64 or any(c not in "0123456789abcdef" for c in body.token_sha256):
            raise ConnectorError("invalid_token_hash", "token_sha256 must be a lowercase hex SHA-256 digest.", 422)
        existing = self.profiles.get(profile_id)
        state = ProfileState(
            profile_id=profile_id,
            agent_id=body.agent_id,
            token_hash=body.token_sha256,
            allowed_origins=origins,
            real_enabled=body.real_enabled,
            min_interval_seconds=body.min_interval_seconds,
            daily_cap=body.daily_cap,
            dom={**DEFAULT_DOM, **(body.dom or {})},
            stop_selectors=list(dict.fromkeys(DEFAULT_STOP_SELECTORS + list(body.extra_stop_selectors or []))),
            stop_phrases=list(dict.fromkeys(DEFAULT_STOP_PHRASES + list(body.extra_stop_phrases or []))),
        )
        if existing is not None:
            # Counters survive re-registration so a token rotation cannot reset the daily cap.
            state.day, state.count_today = existing.day, existing.count_today
            state.last_request_end, state.requests_total = existing.last_request_end, existing.requests_total
            if existing.status == "needs_human":
                state.status, state.needs_human_reason = "needs_human", existing.needs_human_reason
            if existing.ws is not None and existing.token_hash == state.token_hash and existing.origin in origins:
                state.ws, state.connection_id, state.origin = existing.ws, existing.connection_id, existing.origin
                state.last_seen, state.script_version = existing.last_seen, existing.script_version
                if state.status != "needs_human":
                    state.status = existing.status
            elif existing.ws is not None:
                asyncio.ensure_future(self._close(existing, 4401, "token or origin changed"))
        self.profiles[profile_id] = state
        logger.info("connector profile registered profile=%s origins=%d real=%s", profile_id, len(origins), body.real_enabled)
        return state

    async def unregister(self, profile_id: str) -> bool:
        state = self.profiles.pop(profile_id, None)
        if state is None:
            return False
        self._fail_pending(state, ConnectorError("offline", "Profile was unregistered.", 503))
        if state.ws is not None:
            await self._close(state, 4410, "unregistered")
        return True

    def reset(self, profile_id: str) -> ProfileState:
        state = self._get(profile_id)
        state.needs_human_reason = None
        state.status = "ready" if state.ws is not None else "offline"
        logger.info("connector profile reset by human profile=%s status=%s", profile_id, state.status)
        return state

    def _get(self, profile_id: str) -> ProfileState:
        state = self.profiles.get(profile_id)
        if state is None:
            raise ConnectorError("not_registered", "Profile is not registered with the connector.", 404)
        return state

    # ------------------------------------------------------------------ websocket
    async def handle_socket(self, websocket: WebSocket) -> None:
        await websocket.accept()
        state: ProfileState | None = None
        connection_id = uuid.uuid4().hex
        try:
            try:
                hello = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=HELLO_TIMEOUT_SECONDS))
            except (asyncio.TimeoutError, TimeoutError, ValueError):
                await websocket.close(code=4400)
                return
            state = self._authenticate(hello, websocket.headers.get("origin"))
            if state is None:
                await websocket.close(code=4401)
                return
            if state.ws is not None:
                await self._close(state, 4409, "replaced by a newer connection")
            state.ws, state.connection_id = websocket, connection_id
            state.origin = _origin_of(str(hello.get("page_origin") or ""))
            state.script_version = str(hello.get("script_version") or "")[:32] or None
            state.last_seen = time.monotonic()
            if state.status != "needs_human":
                state.status = "ready"
            await websocket.send_text(json.dumps({
                "type": "config",
                "profile_id": state.profile_id,
                "dom": state.dom,
                "stop_selectors": state.stop_selectors,
                "stop_phrases": state.stop_phrases,
                "reply_timeout_seconds": float(os.environ.get("ARENA_REPLY_TIMEOUT_SECONDS", "600") or 600),
                "status": state.status,
            }))
            logger.info("connector connected profile=%s", state.profile_id)
            while True:
                message = json.loads(await websocket.receive_text())
                state.last_seen = time.monotonic()
                await self._on_message(state, message)
        except WebSocketDisconnect:
            pass
        except (ValueError, TypeError):
            with contextlib.suppress(Exception):
                await websocket.close(code=4400)
        finally:
            if state is not None and state.connection_id == connection_id:
                state.ws, state.connection_id = None, None
                if state.status != "needs_human":
                    state.status = "offline"
                self._fail_pending(state, ConnectorError("offline", "Browser profile disconnected.", 503))
                logger.info("connector disconnected profile=%s", state.profile_id)

    def _authenticate(self, hello: dict[str, Any], ws_origin: str | None) -> ProfileState | None:
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            return None
        state = self.profiles.get(str(hello.get("profile_id") or ""))
        if state is None:
            return None
        token = str(hello.get("token") or "")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not token or not hmac.compare_digest(digest, state.token_hash):
            logger.warning("connector auth failed profile=%s", state.profile_id)
            return None
        page_origin = _origin_of(str(hello.get("page_origin") or ""))
        # Browsers set the WebSocket Origin header to the page origin; both must be allowed.
        header_origin = _origin_of(ws_origin or "") if ws_origin else page_origin
        if page_origin not in state.allowed_origins or header_origin != page_origin:
            logger.warning("connector origin refused profile=%s", state.profile_id)
            return None
        if not _is_loopback_origin(page_origin) and not (state.real_enabled and real_arena_allowed_by_bridge()):
            logger.warning("connector real-Arena origin refused (not enabled) profile=%s", state.profile_id)
            return None
        return state

    async def _on_message(self, state: ProfileState, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "status":
            new = str(message.get("status") or "")
            if new == "needs_human":
                self._mark_needs_human(state, str(message.get("reason") or "human check"))
            elif new in ("ready", "busy") and state.status != "needs_human":
                state.status = new
        elif kind == "needs_human":
            self._mark_needs_human(state, str(message.get("reason") or "human check"))
        elif kind == "answer":
            future = state.pending.get(str(message.get("request_id") or ""))
            if future is not None and not future.done():
                future.set_result(str(message.get("text") or "")[:MAX_ANSWER_CHARS])
        elif kind == "error":
            future = state.pending.get(str(message.get("request_id") or ""))
            if future is not None and not future.done():
                future.set_exception(ConnectorError(
                    str(message.get("code") or "connector_error")[:48],
                    str(message.get("message") or "Userscript reported an error.")[:300],
                    502,
                ))

    def _mark_needs_human(self, state: ProfileState, reason: str) -> None:
        state.status = "needs_human"
        state.needs_human_reason = reason[:200]
        logger.warning("connector needs_human profile=%s", state.profile_id)
        self._fail_pending(state, ConnectorError("needs_human", f"Profile needs a human: {state.needs_human_reason}", 409))

    @staticmethod
    def _fail_pending(state: ProfileState, error: ConnectorError) -> None:
        for future in list(state.pending.values()):
            if not future.done():
                future.set_exception(error)

    @staticmethod
    async def _close(state: ProfileState, code: int, reason: str) -> None:
        ws = state.ws
        state.ws, state.connection_id = None, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close(code=code, reason=reason)

    # ------------------------------------------------------------------ dispatch
    async def dispatch(self, profile_id: str, prompt: str, *, timeout: float) -> str:
        state = self._get(profile_id)
        if not _is_loopback_origin(state.origin or "http://127.0.0.1") and not (state.real_enabled and real_arena_allowed_by_bridge()):
            raise ConnectorError("not_enabled", "Real Arena use is not enabled.", 403)
        if state.status == "needs_human":
            raise ConnectorError("needs_human", f"Profile needs a human: {state.needs_human_reason}", 409)
        if state.ws is None:
            raise ConnectorError("offline", "The browser profile's userscript is not connected.", 503)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ConnectorError("prompt_too_large", "Prompt exceeds the connector limit.", 413)
        async with state.lock:  # exactly one in-flight request per profile
            state._roll_day()
            if state.count_today >= state.daily_cap:
                raise ConnectorError("daily_cap_reached", "Daily request cap for this profile reached.", 429)
            wait = state.last_request_end + state.min_interval_seconds - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if state.status == "needs_human":
                raise ConnectorError("needs_human", f"Profile needs a human: {state.needs_human_reason}", 409)
            if state.ws is None:
                raise ConnectorError("offline", "The browser profile's userscript is not connected.", 503)
            request_id = uuid.uuid4().hex
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            state.pending[request_id] = future
            state.count_today += 1
            state.requests_total += 1
            state.status = "busy"
            started = time.monotonic()
            try:
                await state.ws.send_text(json.dumps({"type": "prompt", "request_id": request_id, "prompt": prompt}))
                answer = await asyncio.wait_for(future, timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise ConnectorError("timeout", "The Arena page did not finish answering in time.", 504) from exc
            finally:
                state.pending.pop(request_id, None)
                state.last_request_end = time.monotonic()
                if state.status == "busy":
                    state.status = "ready" if state.ws is not None else "offline"
            logger.info(
                "connector answer profile=%s prompt_chars=%d answer_chars=%d seconds=%.1f",
                profile_id, len(prompt), len(answer), time.monotonic() - started,
            )
            return answer

    def summaries(self) -> list[dict[str, Any]]:
        return [s.summary() for s in sorted(self.profiles.values(), key=lambda s: s.profile_id)]


class ProfileRegistration(BaseModel):
    token_sha256: str = Field(min_length=64, max_length=64)
    agent_id: str | None = Field(default=None, max_length=64)
    allowed_origins: list[str] = Field(min_length=1, max_length=8)
    real_enabled: bool = False
    min_interval_seconds: float = Field(default=20.0, ge=0, le=3600)
    daily_cap: int = Field(default=100, ge=1, le=5000)
    dom: dict[str, str] | None = None
    extra_stop_selectors: list[str] | None = Field(default=None, max_length=50)
    extra_stop_phrases: list[str] | None = Field(default=None, max_length=50)


def _require_admin_bearer(authorization: str | None) -> None:
    expected = os.environ.get("ARENA_BRIDGE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail={"code": "not_configured", "message": "ARENA_BRIDGE_TOKEN is required for the connector."})
    if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(authorization[7:].strip(), expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bridge token.")


def _admin_dep():
    from fastapi import Header

    def dep(authorization: str | None = Header(default=None)) -> None:
        _require_admin_bearer(authorization)

    return dep


def _http_error(exc: ConnectorError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


def register_connector_routes(app: FastAPI, hub: ConnectorHub) -> None:
    admin = Depends(_admin_dep())

    @app.websocket("/v1/connector")
    async def connector_socket(websocket: WebSocket) -> None:
        await hub.handle_socket(websocket)

    @app.put("/v1/connector/profiles/{profile_id}", dependencies=[admin])
    async def register_profile(profile_id: str, body: ProfileRegistration) -> dict[str, Any]:
        try:
            return hub.register(profile_id, body).summary()
        except ConnectorError as exc:
            raise _http_error(exc) from exc

    @app.delete("/v1/connector/profiles/{profile_id}", dependencies=[admin])
    async def unregister_profile(profile_id: str) -> dict[str, Any]:
        return {"profile_id": profile_id, "unregistered": await hub.unregister(profile_id)}

    @app.post("/v1/connector/profiles/{profile_id}/reset", dependencies=[admin])
    async def reset_profile(profile_id: str) -> dict[str, Any]:
        try:
            return hub.reset(profile_id).summary()
        except ConnectorError as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/connector/profiles", dependencies=[admin])
    async def list_profiles() -> dict[str, Any]:
        return {"real_arena_allowed": real_arena_allowed_by_bridge(), "profiles": hub.summaries()}
