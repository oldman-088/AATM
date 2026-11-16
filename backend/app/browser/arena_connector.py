"""Arena browser connector (backend side).

The human signs in to Arena manually in each employee's own Chrome profile and installs the
userscript generated here (after reviewing it). The userscript talks only to the local bridge
(``ws://127.0.0.1:4100/v1/connector``) with a per-profile token. AATM never injects scripts
(``BrowserManager.run_userscript`` stays 501), never reads cookies/tokens, and never solves or
bypasses a human check: the profile is stopped as ``needs_human`` until a person resumes it.

Real Arena is used when ``ARENA_CONNECTOR_ENABLED`` is true; the userscript then runs on the
origins listed in ``ARENA_ALLOWED_ORIGINS``. There are no authorization references, expiry dates,
request intervals, usage quotas or profile-count limits: every employee profile with an installed
userscript is an independent Arena worker. ``ARENA_MOCK_URL`` (loopback only) allows full
end-to-end testing against the local mock page at any time.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.models import Agent, AIProvider, BrowserProfile

SCRIPT_VERSION = "2.0.0"
ARENA_BROWSER_PROVIDER_ID = "arena-browser"
ARENA_MODES = ("chat", "agent")
DOM_KEYS = ("input", "send", "assistant_message", "user_message", "busy")
NOT_ENABLED_MESSAGE = (
    "Arena connector is not enabled: set ARENA_CONNECTOR_ENABLED=true (with ARENA_ALLOWED_ORIGINS), "
    "or set ARENA_MOCK_URL to test against the local mock page."
)


class ArenaNotEnabled(Exception):
    code = "not_enabled"


def _origin(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"


def _is_loopback(url: str) -> bool:
    host = urlparse(url or "").hostname or ""
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def configured_origins() -> list[str]:
    """Valid origins from ARENA_ALLOWED_ORIGINS (bare hosts are read as https)."""
    origins: list[str] = []
    for raw in (get_settings().ARENA_ALLOWED_ORIGINS or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        origin = _origin(raw if "://" in raw else f"https://{raw}")
        if origin:
            origins.append(origin)
    return list(dict.fromkeys(origins))


def real_arena_enabled() -> bool:
    return bool(get_settings().ARENA_CONNECTOR_ENABLED and configured_origins())


def arena_domains() -> list[str]:
    """Host names of the configured Arena origins (kept for callers that need a host)."""
    return [urlparse(o).netloc for o in configured_origins()]


def mock_url() -> str | None:
    url = (get_settings().ARENA_MOCK_URL or "").strip()
    # The mock must be a local page; a non-loopback "mock" would be a back door to a real site.
    return url if url and _origin(url) and _is_loopback(url) else None


def arena_gate() -> dict[str, Any]:
    s = get_settings()
    real = real_arena_enabled()
    mock = mock_url()
    mode = "real" if real else ("mock" if mock else "disabled")
    return {
        "mode": mode,
        "enabled": mode != "disabled",
        "real_enabled": real,
        "connector_enabled": bool(s.ARENA_CONNECTOR_ENABLED),
        "allowed_origins": configured_origins() if real else [],
        "mock_url": mock,
        "mock_url_rejected": bool((s.ARENA_MOCK_URL or "").strip()) and mock is None,
        "domains": arena_domains() if real else [],
        "agent_new_session_url": (s.ARENA_AGENT_NEW_SESSION_URL or "").strip() or None,
        "max_keep_working": s.ARENA_AGENT_MAX_KEEP_WORKING or None,  # None = unlimited
        "code": None if mode != "disabled" else "not_enabled",
        "message": None if mode != "disabled" else NOT_ENABLED_MESSAGE,
    }


def require_real_arena() -> None:
    """Guard for every real-Arena path (navigation, registration with real origins)."""
    if not real_arena_enabled():
        raise ArenaNotEnabled(NOT_ENABLED_MESSAGE)


def allowed_origins() -> list[str]:
    origins: list[str] = []
    mock = mock_url()
    if mock:
        origins.append(_origin(mock))  # type: ignore[arg-type]
    if real_arena_enabled():
        origins.extend(configured_origins())
    return [o for o in dict.fromkeys(origins) if o]


def match_patterns() -> list[str]:
    patterns: list[str] = []
    mock = mock_url()
    if mock:
        parsed = urlparse(mock)
        patterns.append(f"{_origin(mock)}{parsed.path.rstrip('/') or ''}/*")
    if real_arena_enabled():
        patterns.extend(f"{o}/*" for o in configured_origins())
    return list(dict.fromkeys(patterns))


def bridge_ws_url() -> str:
    base = (get_settings().ARENA_BRIDGE_URL or "http://127.0.0.1:4100").rstrip("/")
    return ("wss://" + base[len("https://"):] if base.startswith("https://") else "ws://" + base[len("http://"):]) + "/v1/connector"


def mint_token(profile: BrowserProfile) -> str:
    """Create (or rotate) the per-profile userscript token. Only its SHA-256 is stored."""
    token = secrets.token_urlsafe(32)
    profile.connector_token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    profile.connector_token_rotated_at = datetime.now(timezone.utc)
    return token


def build_userscript(profile: BrowserProfile, agent: Agent | None, token: str) -> str:
    """Generate the reviewable userscript for one profile. Minimal grants, scoped @match, no storage access."""
    patterns = match_patterns()
    if not patterns:
        raise ArenaNotEnabled(NOT_ENABLED_MESSAGE)
    header = "\n".join(
        [
            "// ==UserScript==",
            f"// @name         AATM Arena Connector - {profile.id}",
            "// @namespace    aatm.arena.connector",
            f"// @version      {SCRIPT_VERSION}",
            f"// @description  Sends one AATM prompt at a time to this page for {agent.name if agent else profile.id} and returns the complete answer. Stops on any human check.",
            *[f"// @match        {p}" for p in patterns],
            "// @grant        none",
            "// @run-at       document-idle",
            "// @noframes",
            "// ==/UserScript==",
        ]
    )
    config = {
        "profileId": profile.id,
        "token": token,
        "bridge": bridge_ws_url(),
        "version": SCRIPT_VERSION,
    }
    return header + "\n\n" + USERSCRIPT_BODY.replace("__AATM_CONFIG__", json.dumps(config))


# The body only: reads the page's chat DOM (selectors come from the bridge config), types the
# prompt, waits for the answer to be complete and stable, and reports. It never touches cookies,
# storage, credentials or network APIs other than the one local WebSocket.
USERSCRIPT_BODY = r"""(function () {
  "use strict";
  var CFG = __AATM_CONFIG__;
  var socket = null;
  var settings = null;
  var busy = false;
  var stopped = false;
  var retryMs = 2000;

  function q(sel) { try { return sel ? document.querySelector(sel) : null; } catch (e) { return null; } }
  function qa(sel) { try { return sel ? Array.prototype.slice.call(document.querySelectorAll(sel)) : []; } catch (e) { return []; } }
  function send(obj) { if (socket && socket.readyState === 1) socket.send(JSON.stringify(obj)); }
  function status(s, reason) { send({ type: "status", status: s, reason: reason || null }); }

  function humanCheckReason() {
    if (!settings) return null;
    for (var i = 0; i < settings.stop_selectors.length; i++) {
      if (q(settings.stop_selectors[i])) return "human check element on page";
    }
    var text = (document.body && document.body.innerText || "");
    var transcript = qa(settings.dom.assistant_message).concat(qa(settings.dom.user_message));
    for (var j = 0; j < transcript.length; j++) { text = text.split(transcript[j].innerText || "").join(" "); }
    text = text.toLowerCase();
    for (var k = 0; k < settings.stop_phrases.length; k++) {
      if (text.indexOf(settings.stop_phrases[k]) !== -1) return "page shows: " + settings.stop_phrases[k];
    }
    return null;
  }

  function stopForHuman(reason, requestId) {
    stopped = true;
    busy = false;
    send({ type: "needs_human", reason: reason, request_id: requestId || null });
  }

  function typeInto(el, text) {
    el.focus();
    var proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) { desc.set.call(el, text); } else { el.value = text; }
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function lastAnswer() {
    var all = qa(settings.dom.assistant_message);
    return { count: all.length, text: all.length ? (all[all.length - 1].innerText || "") : "" };
  }

  function handlePrompt(msg) {
    if (stopped) { send({ type: "error", request_id: msg.request_id, code: "needs_human", message: "stopped for a human" }); return; }
    if (busy) { send({ type: "error", request_id: msg.request_id, code: "busy", message: "one request at a time" }); return; }
    var reason = humanCheckReason();
    if (reason) { stopForHuman(reason, msg.request_id); return; }
    var input = q(settings.dom.input);
    var button = q(settings.dom.send);
    if (!input || !button) { stopForHuman("chat input not found (page changed or signed out)", msg.request_id); return; }
    busy = true;
    status("busy");
    var before = lastAnswer().count;
    typeInto(input, msg.prompt);
    setTimeout(function () { button.click(); }, 150);
    var started = Date.now();
    var lastText = null;
    var stableSince = 0;
    var timeoutMs = (settings.reply_timeout_seconds || 600) * 1000;
    var timer = setInterval(function () {
      var r = humanCheckReason();
      if (r) { clearInterval(timer); stopForHuman(r, msg.request_id); return; }
      if (Date.now() - started > timeoutMs) {
        clearInterval(timer); busy = false; status("ready");
        send({ type: "error", request_id: msg.request_id, code: "timeout", message: "no complete answer in time" });
        return;
      }
      var a = lastAnswer();
      var generating = !!q(settings.dom.busy);
      if (a.count > before && !generating && a.text) {
        if (a.text === lastText) {
          if (Date.now() - stableSince >= 1000) {
            clearInterval(timer); busy = false;
            send({ type: "answer", request_id: msg.request_id, text: a.text });
            status("ready");
          }
        } else { lastText = a.text; stableSince = Date.now(); }
      }
    }, 250);
  }

  function connect() {
    socket = new WebSocket(CFG.bridge);
    socket.onopen = function () {
      retryMs = 2000;
      send({ type: "hello", profile_id: CFG.profileId, token: CFG.token, page_origin: location.origin, script_version: CFG.version });
    };
    socket.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "config") {
        settings = msg;
        stopped = msg.status === "needs_human";
        var reason = humanCheckReason();
        if (reason) { stopForHuman(reason); } else if (!stopped) { status("ready"); }
      } else if (msg.type === "prompt") {
        handlePrompt(msg);
      }
    };
    socket.onclose = function (ev) {
      socket = null;
      if (ev.code === 4401 || ev.code === 4410) return;  // bad token / unregistered: wait for a new script
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 60000);
    };
  }

  connect();
})();
"""


def _bridge_headers() -> dict[str, str]:
    import os

    token = os.environ.get("ARENA_BRIDGE_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _bridge_base() -> str:
    return (get_settings().ARENA_BRIDGE_URL or "http://127.0.0.1:4100").rstrip("/")


def profile_arena_mode(profile: BrowserProfile) -> str:
    mode = (getattr(profile, "arena_mode", None) or "chat").strip().lower()
    return mode if mode in ARENA_MODES else "chat"


async def register_profile_with_bridge(profile: BrowserProfile, *, dom: dict[str, str] | None = None) -> dict[str, Any]:
    """Tell the bridge which token hash / origins / mode this profile uses."""
    gate = arena_gate()
    if not gate["enabled"]:
        return {"ok": False, "code": "not_enabled", "message": NOT_ENABLED_MESSAGE}
    if not profile.connector_token_hash:
        return {"ok": False, "code": "no_token", "message": "Generate the profile's userscript first."}
    s = get_settings()
    body: dict[str, Any] = {
        "token_sha256": profile.connector_token_hash,
        "agent_id": profile.agent_id,
        "allowed_origins": allowed_origins(),
        "real_enabled": gate["real_enabled"],
        "mode": profile_arena_mode(profile),
        "new_session_url": (s.ARENA_AGENT_NEW_SESSION_URL or "").strip() or None,
    }
    configured_dom: dict[str, str] = {}
    raw_dom = (s.ARENA_DOM_SELECTORS or "").strip()
    if raw_dom:
        try:
            parsed = json.loads(raw_dom)
            if isinstance(parsed, dict):
                configured_dom = {str(k): str(v) for k, v in parsed.items() if k in DOM_KEYS}
        except ValueError:
            return {"ok": False, "code": "invalid_dom_selectors", "message": "ARENA_DOM_SELECTORS must be a JSON object."}
    merged_dom = {**configured_dom, **(dom or {})}
    if merged_dom:
        body["dom"] = merged_dom
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.put(f"{_bridge_base()}/v1/connector/profiles/{profile.id}", json=body, headers=_bridge_headers())
    except httpx.HTTPError as exc:
        return {"ok": False, "code": "bridge_unreachable", "message": f"Local bridge unreachable: {type(exc).__name__}"}
    if res.status_code >= 400:
        detail = _detail(res)
        return {"ok": False, "code": detail.get("code", f"http_{res.status_code}"), "message": detail.get("message", "")}
    return {"ok": True, **res.json()}


async def bridge_call(method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 10) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.request(method, f"{_bridge_base()}{path}", headers=_bridge_headers(), json=body)
    except httpx.HTTPError as exc:
        return {"ok": False, "code": "bridge_unreachable", "message": f"Local bridge unreachable: {type(exc).__name__}"}
    if res.status_code >= 400:
        detail = _detail(res)
        return {"ok": False, "code": detail.get("code", f"http_{res.status_code}"), "message": detail.get("message", "")}
    return {"ok": True, **res.json()}


def _detail(res: httpx.Response) -> dict[str, Any]:
    try:
        detail = res.json().get("detail")
    except ValueError:
        return {}
    if isinstance(detail, dict):
        return detail
    return {"message": str(detail)[:300]} if detail else {}


async def ensure_arena_browser_provider(db: Any) -> AIProvider:
    """Create the ``arena-browser`` provider on first use (only once the connector is enabled).

    Several workers can reach this at the same moment, so the row is created in its own session
    and a concurrent duplicate insert is treated as success.
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    provider = await db.get(AIProvider, ARENA_BROWSER_PROVIDER_ID)
    if provider is not None:
        return provider
    async with AsyncSession(db.bind, expire_on_commit=False) as creator:
        creator.add(
            AIProvider(
                id=ARENA_BROWSER_PROVIDER_ID,
                name="Arena (browser connector)",
                provider_type="arena_browser",
                endpoint_url=get_settings().ARENA_BRIDGE_URL,
                credential_ref="env:ARENA_BRIDGE_TOKEN",
                default_model="arena_browser",
                available_models=["arena_browser"],
                priority=9,
                is_enabled=True,
                health_status="unknown",
                # Arena web usage records $0 provider cost; a 0 cap would block every call (spend >= cap).
                daily_limit_usd=1.0,
            )
        )
        try:
            await creator.commit()
        except IntegrityError:
            await creator.rollback()
    provider = await db.get(AIProvider, ARENA_BROWSER_PROVIDER_ID, populate_existing=True)
    assert provider is not None
    return provider
