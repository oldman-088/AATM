"""
AATM Compliant Reference Arena Local Bridge Daemon
==================================================
Listens on 127.0.0.1:4100 by default and implements the official loopback contract
consumed by `backend/app/providers/adapters.py::ArenaLocalBridgeAdapter`:

- GET  /health
- POST /v1/chat/completions
- GET  /v1/sessions
- GET  /v1/sessions/{session_id}
- DELETE /v1/sessions/{session_id}

Compliance & Security Rules:
- Enforces Bearer token authentication via the `ARENA_BRIDGE_TOKEN` environment variable.
- Maintains strict per-employee isolated session context keyed by `session_id` and scoped
  by `account_id` (cross-account session access returns HTTP 403).
- NEVER performs CAPTCHA bypass, anti-bot evasion, or undocumented API scraping.
- Optional upstream forwarding (`ARENA_BRIDGE_UPSTREAM_URL`) only targets an explicitly
  configured official/local OpenAI-compatible HTTP(S) endpoint.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

DEFAULT_MODELS = [
    "arena-bridge-default",
    "arena-reasoning-pro",
    "arena-fast",
]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _validate_http_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Upstream URL must use http:// or https:// with a valid host.")
    return url.strip().rstrip("/")


class ChatMessageInput(BaseModel):
    role: str = Field(pattern="^(system|user|assistant|tool)$")
    content: str = ""


class ChatCompletionRequest(BaseModel):
    model: str = "arena-bridge-default"
    messages: list[ChatMessageInput] = Field(min_length=1)
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    user: str | None = None
    session_id: str | None = None
    account_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class BridgeSessionState:
    session_id: str
    account_id: str | None = None
    agent_id: str | None = None
    conversation_id: str | None = None
    project_id: str | None = None
    model: str = "arena-bridge-default"
    messages: list[dict[str, str]] = field(default_factory=list)
    turn_count: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    created_at: str = field(default_factory=_utcnow_iso)
    last_active_at: str = field(default_factory=_utcnow_iso)

    def to_summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "account_id": self.account_id,
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
            "project_id": self.project_id,
            "model": self.model,
            "turn_count": self.turn_count,
            "message_count": len(self.messages),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
        }

    def to_detail(self) -> dict[str, Any]:
        detail = self.to_summary()
        detail["history"] = list(self.messages)
        return detail


class BridgeSessionStore:
    """Async-safe per-employee session context store with cross-account isolation."""

    def __init__(self, max_history: int = 40) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, BridgeSessionState] = {}
        self.max_history = max(4, max_history)

    async def get_or_create(
        self,
        *,
        session_id: str,
        account_id: str | None,
        agent_id: str | None,
        conversation_id: str | None,
        project_id: str | None,
        model: str,
    ) -> BridgeSessionState:
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                if existing.account_id and account_id and existing.account_id != account_id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Cross-account session access denied: session_id is bound to a different account_id.",
                    )
                if not existing.account_id and account_id:
                    existing.account_id = account_id
                if not existing.agent_id and agent_id:
                    existing.agent_id = agent_id
                if not existing.conversation_id and conversation_id:
                    existing.conversation_id = conversation_id
                if not existing.project_id and project_id:
                    existing.project_id = project_id
                existing.model = model
                return existing

            created = BridgeSessionState(
                session_id=session_id,
                account_id=account_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                project_id=project_id,
                model=model,
            )
            self._sessions[session_id] = created
            return created

    async def record_turn(
        self,
        *,
        session_id: str,
        user_messages: list[dict[str, str]],
        assistant_content: str,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
    ) -> BridgeSessionState:
        async with self._lock:
            sess = self._sessions[session_id]
            for msg in user_messages:
                if msg.get("role") == "system" and any(m.get("role") == "system" for m in sess.messages):
                    continue
                if msg.get("content"):
                    sess.messages.append({"role": msg["role"], "content": msg["content"]})
            sess.messages.append({"role": "assistant", "content": assistant_content})
            if len(sess.messages) > self.max_history:
                # Preserve the first system message (if present) alongside the most recent turns
                system_msgs = [m for m in sess.messages[:1] if m.get("role") == "system"]
                keep_tail = self.max_history - len(system_msgs)
                sess.messages = system_msgs + sess.messages[-keep_tail:]
            sess.turn_count += 1
            sess.total_prompt_tokens += prompt_tokens
            sess.total_completion_tokens += completion_tokens
            sess.model = model
            sess.last_active_at = _utcnow_iso()
            return sess

    async def get(self, session_id: str, account_id: str | None = None) -> BridgeSessionState | None:
        async with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            if sess.account_id and account_id and sess.account_id != account_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cross-account session read denied: session_id is bound to a different account_id.",
                )
            return sess

    async def list_summaries(self, account_id: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            items = []
            for sess in self._sessions.values():
                if account_id and sess.account_id != account_id:
                    continue
                items.append(sess.to_summary())
            return items

    async def delete(self, session_id: str, account_id: str | None = None) -> bool:
        async with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return False
            if sess.account_id and account_id and sess.account_id != account_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cross-account session delete denied: session_id is bound to a different account_id.",
                )
            del self._sessions[session_id]
            return True

    async def count(self) -> int:
        async with self._lock:
            return len(self._sessions)


def _verify_bearer_auth(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("ARENA_BRIDGE_TOKEN", "").strip()
    require_auth = os.environ.get("ARENA_BRIDGE_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes")

    if not expected:
        if require_auth:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ARENA_BRIDGE_TOKEN is not configured while ARENA_BRIDGE_REQUIRE_AUTH=true.",
            )
        return

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    provided = authorization[7:].strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Arena Local Bridge Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _generate_upstream_completion(
    upstream_url: str,
    model: str,
    history_messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> tuple[str, int, int]:
    """Forward isolated session context to an explicitly configured official OpenAI-compatible endpoint."""
    base = _validate_http_url(upstream_url)
    headers = {"Content-Type": "application/json"}
    upstream_key = os.environ.get("ARENA_BRIDGE_UPSTREAM_API_KEY", "").strip()
    if upstream_key:
        headers["Authorization"] = f"Bearer {upstream_key}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{base}/v1/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": history_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        content = ""
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else {}
            if isinstance(msg, dict):
                content = str(msg.get("content", ""))
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_text = "\n".join(m.get("content", "") for m in history_messages)
        prompt_tokens = int(usage.get("prompt_tokens", _estimate_tokens(prompt_text)))
        completion_tokens = int(usage.get("completion_tokens", _estimate_tokens(content)))
        return content, prompt_tokens, completion_tokens


def _generate_local_reference_completion(
    session: BridgeSessionState,
    incoming_messages: list[dict[str, str]],
    model: str,
) -> tuple[str, int, int]:
    """Safe zero-spend deterministic local runtime preserving per-employee isolated session context."""
    user_texts = [m["content"] for m in incoming_messages if m.get("role") == "user" and m.get("content")]
    latest_prompt = user_texts[-1] if user_texts else (incoming_messages[-1].get("content", "") if incoming_messages else "")
    prior_turns = session.turn_count
    prompt_context = "\n".join(m.get("content", "") for m in session.messages) + "\n" + latest_prompt
    prompt_tokens = _estimate_tokens(prompt_context)

    summary_snippet = latest_prompt[:180] + ("..." if len(latest_prompt) > 180 else "")
    acct_label = session.account_id or "default"
    agent_label = session.agent_id or "unassigned"
    content = (
        f"[Arena Local Bridge | {model} | session={session.session_id} | account={acct_label} "
        f"| agent={agent_label} | turn={prior_turns + 1}] {summary_snippet}"
    )
    completion_tokens = _estimate_tokens(content)
    return content, prompt_tokens, completion_tokens


def create_app(max_history: int | None = None) -> FastAPI:
    hist_limit = max_history if max_history is not None else int(os.environ.get("ARENA_BRIDGE_MAX_HISTORY", "40"))
    store = BridgeSessionStore(max_history=hist_limit)
    started_at = time.monotonic()

    bridge_app = FastAPI(
        title="AATM Arena Local Bridge (Reference Daemon)",
        version="2.1.0",
        description="Compliant loopback bridge daemon for AATM Division 05 with Bearer auth and per-employee session isolation.",
    )
    bridge_app.state.session_store = store

    @bridge_app.get("/health", dependencies=[Depends(_verify_bearer_auth)])
    async def health() -> dict[str, Any]:
        token_configured = bool(os.environ.get("ARENA_BRIDGE_TOKEN", "").strip())
        upstream = os.environ.get("ARENA_BRIDGE_UPSTREAM_URL", "").strip() or None
        return {
            "status": "ok",
            "service": "aatm-arena-local-bridge",
            "version": "2.1.0",
            "mode": "upstream_proxy" if upstream else "local_reference",
            "auth_required": token_configured,
            "models": list(DEFAULT_MODELS),
            "active_sessions": await store.count(),
            "uptime_seconds": round(time.monotonic() - started_at, 2),
        }

    @bridge_app.post("/v1/chat/completions", dependencies=[Depends(_verify_bearer_auth)])
    async def chat_completions(
        body: ChatCompletionRequest,
        x_aatm_session_id: str | None = Header(default=None),
        x_aatm_account_id: str | None = Header(default=None),
        x_aatm_agent_id: str | None = Header(default=None),
        x_aatm_conversation_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        meta = dict(body.metadata or {})
        session_id = (
            (x_aatm_session_id or "").strip()
            or (body.session_id or "").strip()
            or str(meta.get("session_id") or "").strip()
            or f"bridge-sess-{uuid.uuid4().hex[:10]}"
        )
        account_id = (
            (x_aatm_account_id or "").strip()
            or (body.account_id or "").strip()
            or str(meta.get("account_id") or "").strip()
            or None
        )
        agent_id = (
            (x_aatm_agent_id or "").strip()
            or (body.user or "").strip()
            or str(meta.get("agent_id") or "").strip()
            or None
        )
        conversation_id = (
            (x_aatm_conversation_id or "").strip()
            or str(meta.get("conversation_id") or "").strip()
            or None
        )
        project_id = str(meta.get("project_id") or "").strip() or None

        session = await store.get_or_create(
            session_id=session_id,
            account_id=account_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            project_id=project_id,
            model=body.model,
        )

        incoming = [{"role": m.role, "content": m.content} for m in body.messages]
        upstream_url = os.environ.get("ARENA_BRIDGE_UPSTREAM_URL", "").strip()
        if upstream_url:
            combined_history = list(session.messages) + [m for m in incoming if m.get("content")]
            try:
                content, prompt_tokens, completion_tokens = await _generate_upstream_completion(
                    upstream_url=upstream_url,
                    model=body.model,
                    history_messages=combined_history,
                    max_tokens=body.max_tokens,
                    temperature=body.temperature,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Upstream completion error: {type(exc).__name__}",
                ) from exc
        else:
            content, prompt_tokens, completion_tokens = _generate_local_reference_completion(
                session=session,
                incoming_messages=incoming,
                model=body.model,
            )

        updated = await store.record_turn(
            session_id=session.session_id,
            user_messages=incoming,
            assistant_content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=body.model,
        )

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "session": updated.to_summary(),
        }

    @bridge_app.get("/v1/sessions", dependencies=[Depends(_verify_bearer_auth)])
    async def list_sessions(
        x_aatm_account_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        account_filter = (x_aatm_account_id or "").strip() or None
        sessions = await store.list_summaries(account_id=account_filter)
        return {"count": len(sessions), "sessions": sessions}

    @bridge_app.get("/v1/sessions/{session_id}", dependencies=[Depends(_verify_bearer_auth)])
    async def get_session(
        session_id: str,
        x_aatm_account_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        account_id = (x_aatm_account_id or "").strip() or None
        sess = await store.get(session_id, account_id=account_id)
        if sess is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session '{session_id}' not found.")
        return sess.to_detail()

    @bridge_app.delete("/v1/sessions/{session_id}", dependencies=[Depends(_verify_bearer_auth)])
    async def delete_session(
        session_id: str,
        x_aatm_account_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        account_id = (x_aatm_account_id or "").strip() or None
        deleted = await store.delete(session_id, account_id=account_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session '{session_id}' not found.")
        return {"deleted": True, "session_id": session_id}

    return bridge_app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("ARENA_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("ARENA_BRIDGE_PORT", "4100"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
