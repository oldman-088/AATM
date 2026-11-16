"""Arena browser connector hub: Tampermonkey userscript <-> local bridge.

* ``WS /v1/connector`` - one connection per employee browser profile. The userscript must send a
  ``hello`` with the per-profile token that AATM generated (only its SHA-256 is registered here)
  from an origin that AATM allowed for that profile.
* ``PUT/DELETE /v1/connector/profiles/{id}``, ``POST .../reset``, ``POST .../command``,
  ``GET /v1/connector/profiles[/{id}]`` - backend-only administration, always protected by
  ``ARENA_BRIDGE_TOKEN``.
* ``dispatch()`` is used by ``/v1/chat/completions`` when ``model == "arena_browser"`` (chat mode).
* ``command()`` drives an Arena Agent session (agent mode): ``new_session``, ``submit``,
  ``keep_working``, ``read_last_reply`` and ``selector_test``. The userscript streams the session
  state (``ready``, ``generating``, ``idle``, ``keep_working_visible``, ``needs_human``, ``offline``)
  which the backend reads to orchestrate the task.

Enforced here (and independently in the backend):
* one in-flight request/command per profile (a browser tab can only do one thing at a time) - there
  are no request intervals, daily caps, profile-count limits or other usage quotas;
* a profile that reports ``needs_human`` (human check, verification page, login wall or terms
  prompt) stops immediately and stays stopped until a human resets it - nothing is solved or
  bypassed; only the read-only commands still run while stopped;
* real (non-loopback) Arena origins are accepted only when the backend registered the profile
  with ``real_enabled`` AND this bridge process itself has ``ARENA_CONNECTOR_ENABLED=true``;
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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

logger = logging.getLogger("aatm.bridge.connector")

HELLO_TIMEOUT_SECONDS = 10.0
MAX_ANSWER_CHARS = 200_000
MAX_PROMPT_CHARS = 120_000
MAX_EVENTS = 50

AGENT_STATES = ("ready", "generating", "idle", "keep_working_visible", "needs_human", "offline")
# Commands the bridge may send to an Arena Agent page. Anything else is refused before it
# reaches the browser. Read-only commands still run while the profile waits for a human.
AGENT_COMMANDS = ("new_session", "submit", "keep_working", "read_last_reply", "selector_test")
READ_ONLY_COMMANDS = ("read_last_reply", "selector_test")

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
    "iframe[src*='turnstile']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='recaptcha']",
    "[class*='turnstile']",
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
    "prove you're human",
    "prove you are human",
    "complete the captcha",
    "complete the check",
]
DEFAULT_DOM = {
    "input": "textarea",
    "send": "button[type='submit']",
    "assistant_message": "[data-role='assistant']",
    "user_message": "[data-role='user']",
    "busy": "[data-generating='true']",
}


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def real_arena_allowed_by_bridge() -> bool:
    return _truthy("ARENA_CONNECTOR_ENABLED")


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
    mode: str = "chat"  # chat (one prompt -> one answer) | agent (Arena Agent session)
    new_session_url: str | None = None
    dom: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DOM))
    stop_selectors: list[str] = field(default_factory=lambda: list(DEFAULT_STOP_SELECTORS))
    stop_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_STOP_PHRASES))
    status: str = "offline"  # offline | ready | busy | needs_human
    needs_human_reason: str | None = None
    origin: str | None = None
    script_version: str | None = None
    last_seen: float | None = None
    requests_total: int = 0
    commands_total: int = 0
    ws: WebSocket | None = None
    connection_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    # Arena Agent session state, as reported by the userscript's page monitor.
    agent_state: str = "offline"
    agent_state_since: float | None = None  # wall clock (time.time) of the last state change
    keep_working_visible: bool = False
    keep_working_clicks: int = 0
    last_error: str | None = None
    selector_report: dict[str, Any] | None = None
    connections_total: int = 0
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))

    def health(self) -> str:
        if self.status == "needs_human":
            return "red"
        if self.status == "offline":
            return "red" if self.ws is None and self.last_seen else "yellow"
        return "green"

    def record(self, kind: str, detail: str | None = None) -> None:
        self.events.append({"ts": _iso(time.time()), "kind": kind[:48], "detail": (detail or "")[:200] or None})

    def set_agent_state(self, new: str, detail: str | None = None) -> None:
        if new not in AGENT_STATES:
            return
        self.keep_working_visible = new == "keep_working_visible"
        if new != self.agent_state:
            self.agent_state = new
            self.agent_state_since = time.time()
            self.record(f"state:{new}", detail)

    def summary(self, *, events: int = 0) -> dict[str, Any]:
        data = {
            "profile_id": self.profile_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "health": self.health(),
            "connected": self.ws is not None,
            "needs_human_reason": self.needs_human_reason,
            "origin": self.origin,
            "script_version": self.script_version,
            "last_seen_seconds_ago": round(time.monotonic() - self.last_seen, 1) if self.last_seen else None,
            "in_flight": self.lock.locked(),
            "real_enabled": self.real_enabled,
            "requests_total": self.requests_total,
            "commands_total": self.commands_total,
            "connections_total": self.connections_total,
            "mode": self.mode,
            "agent_state": self.agent_state,
            "agent_state_since": _iso(self.agent_state_since),
            "agent_state_age_seconds": round(time.time() - self.agent_state_since, 1) if self.agent_state_since else None,
            "keep_working_visible": self.keep_working_visible,
            "keep_working_clicks": self.keep_working_clicks,
            "last_error": self.last_error,
        }
        if events:
            data["recent_events"] = list(self.events)[-events:]
            data["selector_report"] = self.selector_report
        return data


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
                        "This bridge process does not have ARENA_CONNECTOR_ENABLED=true; real Arena stays off.",
                        403,
                    )
            origins.append(origin)
        new_session_url = (body.new_session_url or "").strip() or None
        if new_session_url and _origin_of(new_session_url) not in origins:
            raise ConnectorError("invalid_new_session_url", "The new-session URL must be on an allowed Arena origin.", 422)
        if len(body.token_sha256) != 64 or any(c not in "0123456789abcdef" for c in body.token_sha256):
            raise ConnectorError("invalid_token_hash", "token_sha256 must be a lowercase hex SHA-256 digest.", 422)
        existing = self.profiles.get(profile_id)
        state = ProfileState(
            profile_id=profile_id,
            agent_id=body.agent_id,
            token_hash=body.token_sha256,
            allowed_origins=origins,
            real_enabled=body.real_enabled,
            mode=body.mode,
            new_session_url=new_session_url,
            dom={**DEFAULT_DOM, **(body.dom or {})},
            stop_selectors=list(dict.fromkeys(DEFAULT_STOP_SELECTORS + list(body.extra_stop_selectors or []))),
            stop_phrases=list(dict.fromkeys(DEFAULT_STOP_PHRASES + list(body.extra_stop_phrases or []))),
        )
        if existing is not None:
            # Session history survives re-registration (token rotation, backend restart).
            state.requests_total, state.commands_total = existing.requests_total, existing.commands_total
            state.connections_total, state.keep_working_clicks = existing.connections_total, existing.keep_working_clicks
            state.events, state.selector_report, state.last_error = existing.events, existing.selector_report, existing.last_error
            state.agent_state, state.agent_state_since = existing.agent_state, existing.agent_state_since
            state.keep_working_visible = existing.keep_working_visible
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
        if existing is not None and state.ws is None:
            state.set_agent_state("offline", "re-registered")
        logger.info("connector profile registered profile=%s origins=%d real=%s mode=%s", profile_id, len(origins), body.real_enabled, body.mode)
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
        state.last_error = None
        if state.agent_state == "needs_human":
            state.set_agent_state("ready" if state.ws is not None else "offline", "resumed by a human")
        state.record("resumed", "needs_human cleared by a human")
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
            state.connections_total += 1
            if state.status != "needs_human":
                state.status = "ready"
            state.record("connected", f"script {state.script_version or '?'}")
            if state.mode == "agent" and state.agent_state == "offline":
                # The page monitor reports the real state right after the config arrives.
                state.set_agent_state("ready" if state.status != "needs_human" else "needs_human", "reconnected")
            await websocket.send_text(json.dumps({
                "type": "config",
                "profile_id": state.profile_id,
                "dom": state.dom,
                "stop_selectors": state.stop_selectors,
                "stop_phrases": state.stop_phrases,
                "reply_timeout_seconds": float(os.environ.get("ARENA_REPLY_TIMEOUT_SECONDS", "600") or 600),
                "status": state.status,
                "mode": state.mode,
                "commands": list(AGENT_COMMANDS),
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
                if state.agent_state != "needs_human":
                    state.set_agent_state("offline", "browser disconnected")
                state.record("disconnected")
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
        elif kind == "state":
            new = str(message.get("state") or "")
            if new == "needs_human":
                self._mark_needs_human(state, str(message.get("reason") or "human check"))
            elif new in AGENT_STATES and state.status != "needs_human":
                state.set_agent_state(new, str(message.get("reason") or "") or None)
        elif kind == "event":
            event_kind = str(message.get("kind") or "event")
            if event_kind == "keep_working_clicked":
                state.keep_working_clicks += 1
            if event_kind in ("error", "send_failed"):
                state.last_error = str(message.get("detail") or event_kind)[:200]
            state.record(event_kind, str(message.get("detail") or "") or None)
        elif kind == "result":
            future = state.pending.get(str(message.get("request_id") or ""))
            if future is not None and not future.done():
                if message.get("ok"):
                    data = message.get("data")
                    future.set_result(data if isinstance(data, dict) else {})
                else:
                    code = str(message.get("code") or "command_failed")[:48]
                    detail = str(message.get("message") or "The page could not run the command.")[:300]
                    state.last_error = f"{code}: {detail}"[:200]
                    future.set_exception(ConnectorError(code, detail, 502))
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
        state.set_agent_state("needs_human", state.needs_human_reason)
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
            if state.status == "needs_human":
                raise ConnectorError("needs_human", f"Profile needs a human: {state.needs_human_reason}", 409)
            if state.ws is None:
                raise ConnectorError("offline", "The browser profile's userscript is not connected.", 503)
            request_id = uuid.uuid4().hex
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            state.pending[request_id] = future
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
                if state.status == "busy":
                    state.status = "ready" if state.ws is not None else "offline"
            logger.info(
                "connector answer profile=%s prompt_chars=%d answer_chars=%d seconds=%.1f",
                profile_id, len(prompt), len(answer), time.monotonic() - started,
            )
            return answer

    # ------------------------------------------------------------------ Arena Agent commands
    def _validate_command(self, state: ProfileState, command: str, args: dict[str, Any]) -> dict[str, Any]:
        if command not in AGENT_COMMANDS:
            raise ConnectorError("unknown_command", f"Unsupported connector command '{command[:40]}'.", 422)
        clean: dict[str, Any] = {}
        if command == "submit":
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ConnectorError("text_required", "submit needs a non-empty text.", 422)
            if len(text) > MAX_PROMPT_CHARS:
                raise ConnectorError("prompt_too_large", "Prompt exceeds the connector limit.", 413)
            clean["text"] = text
        elif command == "new_session":
            # Navigation only ever targets the URL the backend registered for this profile; a
            # caller-supplied URL is accepted only when it is on one of the profile's origins.
            url = str(args.get("url") or state.new_session_url or "").strip() or None
            if url and _origin_of(url) not in state.allowed_origins:
                raise ConnectorError("invalid_new_session_url", "The new-session URL must be on an allowed Arena origin.", 422)
            clean["url"] = url
        return clean

    async def command(self, profile_id: str, command: str, args: dict[str, Any] | None = None, *, timeout: float = 60.0) -> dict[str, Any]:
        """Run one Arena Agent command on the profile's page and return the page's result."""
        state = self._get(profile_id)
        clean = self._validate_command(state, command, dict(args or {}))
        if not _is_loopback_origin(state.origin or "http://127.0.0.1") and not (state.real_enabled and real_arena_allowed_by_bridge()):
            raise ConnectorError("not_enabled", "Real Arena use is not enabled.", 403)
        if state.status == "needs_human" and command not in READ_ONLY_COMMANDS:
            raise ConnectorError("needs_human", f"Profile needs a human: {state.needs_human_reason}", 409)
        if state.ws is None:
            raise ConnectorError("offline", "The browser profile's userscript is not connected.", 503)
        async with state.lock:  # one command at a time per browser tab
            if state.ws is None:
                raise ConnectorError("offline", "The browser profile's userscript is not connected.", 503)
            if state.status == "needs_human" and command not in READ_ONLY_COMMANDS:
                raise ConnectorError("needs_human", f"Profile needs a human: {state.needs_human_reason}", 409)
            request_id = uuid.uuid4().hex
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            state.pending[request_id] = future
            state.commands_total += 1
            started = time.monotonic()
            state.record(f"command:{command}")
            try:
                await state.ws.send_text(json.dumps({"type": "command", "request_id": request_id, "command": command, "args": clean}))
                result = await asyncio.wait_for(future, timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                state.last_error = f"timeout: {command}"
                raise ConnectorError("timeout", f"The Arena page did not finish '{command}' in time.", 504) from exc
            finally:
                state.pending.pop(request_id, None)
            if command == "selector_test":
                state.selector_report = {k: v for k, v in result.items() if k != "request_id"}
            if command == "keep_working" and result.get("clicked"):
                state.keep_working_clicks += 1
            logger.info("connector command profile=%s command=%s ok seconds=%.1f", profile_id, command, time.monotonic() - started)
            return result

    def summaries(self) -> list[dict[str, Any]]:
        return [s.summary() for s in sorted(self.profiles.values(), key=lambda s: s.profile_id)]


class ProfileRegistration(BaseModel):
    token_sha256: str = Field(min_length=64, max_length=64)
    agent_id: str | None = Field(default=None, max_length=64)
    allowed_origins: list[str] = Field(min_length=1, max_length=8)
    real_enabled: bool = False
    mode: Literal["chat", "agent"] = "chat"
    new_session_url: str | None = Field(default=None, max_length=500)
    dom: dict[str, str] | None = None
    extra_stop_selectors: list[str] | None = Field(default=None, max_length=50)
    extra_stop_phrases: list[str] | None = Field(default=None, max_length=50)


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=40)
    args: dict[str, Any] | None = None
    timeout_seconds: float = Field(default=60.0, gt=0, le=3600)


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

    @app.get("/v1/connector/profiles/{profile_id}", dependencies=[admin])
    async def get_profile(profile_id: str) -> dict[str, Any]:
        try:
            return hub._get(profile_id).summary(events=20)
        except ConnectorError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/connector/profiles/{profile_id}/command", dependencies=[admin])
    async def run_command(profile_id: str, body: CommandRequest) -> dict[str, Any]:
        try:
            result = await hub.command(profile_id, body.command, body.args, timeout=body.timeout_seconds)
        except ConnectorError as exc:
            raise _http_error(exc) from exc
        return {"profile_id": profile_id, "command": body.command, "result": result, "state": hub._get(profile_id).summary()}
