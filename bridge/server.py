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
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

BRIDGE_VERSION = "2.4.0"

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
    stream: bool = False
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
    last_active_epoch: float = field(default_factory=time.time)

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

    def to_record(self) -> dict[str, Any]:
        rec = self.to_detail()
        rec["last_active_epoch"] = self.last_active_epoch
        return rec

    @classmethod
    def from_record(cls, raw: dict[str, Any]) -> "BridgeSessionState":
        last_active_at = str(raw.get("last_active_at") or _utcnow_iso())
        epoch_val = raw.get("last_active_epoch")
        if isinstance(epoch_val, (int, float)):
            last_epoch = float(epoch_val)
        else:
            try:
                last_epoch = datetime.fromisoformat(last_active_at).timestamp()
            except ValueError:
                last_epoch = time.time()

        raw_history = raw.get("history")
        messages: list[dict[str, str]] = []
        if isinstance(raw_history, list):
            for entry in raw_history:
                if isinstance(entry, dict) and entry.get("role") in ("system", "user", "assistant", "tool"):
                    messages.append({"role": str(entry["role"]), "content": str(entry.get("content", ""))})

        return cls(
            session_id=str(raw["session_id"]),
            account_id=str(raw["account_id"]) if raw.get("account_id") else None,
            agent_id=str(raw["agent_id"]) if raw.get("agent_id") else None,
            conversation_id=str(raw["conversation_id"]) if raw.get("conversation_id") else None,
            project_id=str(raw["project_id"]) if raw.get("project_id") else None,
            model=str(raw.get("model") or "arena-bridge-default"),
            messages=messages,
            turn_count=int(raw.get("turn_count") or 0),
            total_prompt_tokens=int(raw.get("total_prompt_tokens") or 0),
            total_completion_tokens=int(raw.get("total_completion_tokens") or 0),
            created_at=str(raw.get("created_at") or _utcnow_iso()),
            last_active_at=last_active_at,
            last_active_epoch=last_epoch,
        )


class BridgeSessionStore:
    """Async-safe per-employee session context store with cross-account isolation, optional persistence & TTL eviction."""

    def __init__(
        self,
        max_history: int = 40,
        state_file: str | Path | None = None,
        session_ttl_seconds: float | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, BridgeSessionState] = {}
        self.max_history = max(4, max_history)
        self.state_file: Path | None = Path(state_file) if state_file else None
        self.session_ttl_seconds: float | None = (
            float(session_ttl_seconds) if session_ttl_seconds is not None and float(session_ttl_seconds) > 0 else None
        )
        self._evicted_sessions: int = 0
        self._load_state_sync()
        self._prune_expired_locked()

    def _load_state_sync(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            raw_text = self.state_file.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        items = payload.get("sessions")
        if not isinstance(items, dict):
            return
        for sid, rec in items.items():
            if not isinstance(rec, dict):
                continue
            rec_copy = dict(rec)
            rec_copy.setdefault("session_id", str(sid))
            try:
                sess = BridgeSessionState.from_record(rec_copy)
                self._sessions[sess.session_id] = sess
            except Exception:
                continue

    def _persist_locked(self) -> None:
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": BRIDGE_VERSION,
            "saved_at": _utcnow_iso(),
            "sessions": {sid: sess.to_record() for sid, sess in self._sessions.items()},
        }
        tmp_path = self.state_file.with_suffix(self.state_file.suffix + f".{os.getpid()}.tmp")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp_path, self.state_file)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _prune_expired_locked(self, now: float | None = None) -> int:
        if self.session_ttl_seconds is None:
            return 0
        current = time.time() if now is None else now
        expired_ids = [
            sid
            for sid, sess in self._sessions.items()
            if (current - sess.last_active_epoch) > self.session_ttl_seconds
        ]
        if not expired_ids:
            return 0
        for sid in expired_ids:
            del self._sessions[sid]
        self._evicted_sessions += len(expired_ids)
        self._persist_locked()
        return len(expired_ids)

    async def prune_expired(self, now: float | None = None) -> int:
        async with self._lock:
            return self._prune_expired_locked(now=now)

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
            self._prune_expired_locked()
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
                existing.last_active_at = _utcnow_iso()
                existing.last_active_epoch = time.time()
                self._persist_locked()
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
            self._persist_locked()
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
            sess.last_active_epoch = time.time()
            self._persist_locked()
            return sess

    async def get(self, session_id: str, account_id: str | None = None) -> BridgeSessionState | None:
        async with self._lock:
            self._prune_expired_locked()
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
            self._prune_expired_locked()
            items = []
            for sess in self._sessions.values():
                if account_id and sess.account_id != account_id:
                    continue
                items.append(sess.to_summary())
            return items

    async def delete(self, session_id: str, account_id: str | None = None) -> bool:
        async with self._lock:
            self._prune_expired_locked()
            sess = self._sessions.get(session_id)
            if sess is None:
                return False
            if sess.account_id and account_id and sess.account_id != account_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cross-account session delete denied: session_id is bound to a different account_id.",
                )
            del self._sessions[session_id]
            self._persist_locked()
            return True

    async def count(self) -> int:
        async with self._lock:
            self._prune_expired_locked()
            return len(self._sessions)

    async def metrics(self, account_id: str | None = None) -> dict[str, Any]:
        """Return aggregate session count & token telemetry strictly scoped to account_id when provided."""
        async with self._lock:
            self._prune_expired_locked()
            scoped = [
                sess
                for sess in self._sessions.values()
                if account_id is None or sess.account_id == account_id
            ]
            accounts_breakdown: dict[str, int] = {}
            agents: set[str] = set()
            total_turns = 0
            total_messages = 0
            total_prompt_tokens = 0
            total_completion_tokens = 0
            for sess in scoped:
                acct_key = sess.account_id or "unassigned"
                accounts_breakdown[acct_key] = accounts_breakdown.get(acct_key, 0) + 1
                if sess.agent_id:
                    agents.add(sess.agent_id)
                total_turns += sess.turn_count
                total_messages += len(sess.messages)
                total_prompt_tokens += sess.total_prompt_tokens
                total_completion_tokens += sess.total_completion_tokens

            return {
                "total_sessions": len(scoped),
                "total_turns": total_turns,
                "total_messages": total_messages,
                "total_prompt_tokens": total_prompt_tokens,
                "total_completion_tokens": total_completion_tokens,
                "unique_accounts": len(accounts_breakdown),
                "unique_agents": len(agents),
                "accounts_breakdown": accounts_breakdown,
                "max_history_per_session": self.max_history,
                "persistence_enabled": self.state_file is not None,
                "session_ttl_seconds": self.session_ttl_seconds,
                "evicted_sessions": self._evicted_sessions if account_id is None else None,
            }


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


def _split_sse_chunks(content: str, chunk_size: int = 32) -> list[str]:
    if not content:
        return []
    return [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]


def _parse_optional_ttl_env() -> float | None:
    raw = os.environ.get("AATM_BRIDGE_SESSION_TTL_SECONDS", "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        return None


def create_app(
    max_history: int | None = None,
    state_file: str | Path | None = None,
    session_ttl_seconds: float | None = None,
) -> FastAPI:
    hist_limit = max_history if max_history is not None else int(os.environ.get("ARENA_BRIDGE_MAX_HISTORY", "40"))
    resolved_state_file = (
        state_file
        if state_file is not None
        else (os.environ.get("AATM_BRIDGE_STATE_FILE", "").strip() or None)
    )
    resolved_ttl = session_ttl_seconds if session_ttl_seconds is not None else _parse_optional_ttl_env()
    store = BridgeSessionStore(
        max_history=hist_limit,
        state_file=resolved_state_file,
        session_ttl_seconds=resolved_ttl,
    )
    started_at = time.monotonic()

    bridge_app = FastAPI(
        title="AATM Arena Local Bridge (Reference Daemon)",
        version=BRIDGE_VERSION,
        description="Compliant loopback bridge daemon for AATM Division 05 with Bearer auth, SSE streaming, optional persistence/TTL & per-employee session isolation.",
    )
    bridge_app.state.session_store = store

    @bridge_app.get("/health", dependencies=[Depends(_verify_bearer_auth)])
    async def health() -> dict[str, Any]:
        token_configured = bool(os.environ.get("ARENA_BRIDGE_TOKEN", "").strip())
        upstream = os.environ.get("ARENA_BRIDGE_UPSTREAM_URL", "").strip() or None
        return {
            "status": "ok",
            "service": "aatm-arena-local-bridge",
            "version": BRIDGE_VERSION,
            "mode": "upstream_proxy" if upstream else "diagnostic_only",
            "diagnostic_only": not bool(upstream),
            "auth_required": token_configured,
            "streaming_supported": True,
            "persistence_enabled": store.state_file is not None,
            "session_ttl_seconds": store.session_ttl_seconds,
            "models": list(DEFAULT_MODELS),
            "active_sessions": await store.count(),
            "uptime_seconds": round(time.monotonic() - started_at, 2),
        }

    @bridge_app.post("/v1/chat/completions", dependencies=[Depends(_verify_bearer_auth)], response_model=None)
    async def chat_completions(
        body: ChatCompletionRequest,
        x_aatm_session_id: str | None = Header(default=None),
        x_aatm_account_id: str | None = Header(default=None),
        x_aatm_agent_id: str | None = Header(default=None),
        x_aatm_conversation_id: str | None = Header(default=None),
    ) -> dict[str, Any] | StreamingResponse:
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
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())

        if upstream_url and body.stream:
            try:
                upstream_base = _validate_http_url(upstream_url)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Invalid upstream URL: {exc}",
                ) from exc

            combined_history = list(session.messages) + [m for m in incoming if m.get("content")]
            prompt_text = "\n".join(m.get("content", "") for m in combined_history)

            async def upstream_sse_events() -> AsyncIterator[str]:
                up_headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
                upstream_key = os.environ.get("ARENA_BRIDGE_UPSTREAM_API_KEY", "").strip()
                if upstream_key:
                    up_headers["Authorization"] = f"Bearer {upstream_key}"

                collected_parts: list[str] = []
                upstream_usage: dict[str, Any] = {}
                finish_reason = "stop"

                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        async with client.stream(
                            "POST",
                            f"{upstream_base}/v1/chat/completions",
                            headers=up_headers,
                            json={
                                "model": body.model,
                                "messages": combined_history,
                                "max_tokens": body.max_tokens,
                                "temperature": body.temperature,
                                "stream": True,
                            },
                        ) as resp:
                            if resp.status_code >= 400:
                                await resp.aread()
                                err_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_ts,
                                    "model": body.model,
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                                    "error": {
                                        "type": "upstream_http_error",
                                        "status_code": resp.status_code,
                                        "message": f"Upstream returned HTTP {resp.status_code} during SSE stream.",
                                    },
                                }
                                yield f"data: {json.dumps(err_chunk)}\n\n"
                                yield "data: [DONE]\n\n"
                                return

                            async for raw_line in resp.aiter_lines():
                                line = raw_line.strip()
                                if not line or not line.startswith("data:"):
                                    continue
                                data_str = line[5:].strip()
                                if data_str == "[DONE]":
                                    break
                                try:
                                    frame = json.loads(data_str)
                                except json.JSONDecodeError:
                                    continue
                                if not isinstance(frame, dict):
                                    continue

                                if isinstance(frame.get("usage"), dict):
                                    upstream_usage = frame["usage"]

                                choices = frame.get("choices")
                                is_terminal_only = False
                                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                                    first = choices[0]
                                    delta_obj = first.get("delta") if isinstance(first.get("delta"), dict) else {}
                                    piece = delta_obj.get("content")
                                    if isinstance(piece, str) and piece:
                                        collected_parts.append(piece)
                                    if first.get("finish_reason"):
                                        finish_reason = str(first["finish_reason"])
                                        if not piece and not delta_obj.get("role"):
                                            is_terminal_only = True
                                        else:
                                            # Forward delta immediately; final chunk with session+usage follows below
                                            frame = dict(frame)
                                            frame["choices"] = [
                                                {**first, "finish_reason": None},
                                                *choices[1:],
                                            ]

                                if not is_terminal_only:
                                    yield f"data: {json.dumps(frame)}\n\n"

                    full_content = "".join(collected_parts)
                    p_tok = (
                        int(upstream_usage["prompt_tokens"])
                        if upstream_usage.get("prompt_tokens") is not None
                        else _estimate_tokens(prompt_text)
                    )
                    c_tok = (
                        int(upstream_usage["completion_tokens"])
                        if upstream_usage.get("completion_tokens") is not None
                        else _estimate_tokens(full_content)
                    )
                    updated_sess = await store.record_turn(
                        session_id=session.session_id,
                        user_messages=incoming,
                        assistant_content=full_content,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        model=body.model,
                    )
                    final_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": body.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                        "usage": {
                            "prompt_tokens": p_tok,
                            "completion_tokens": c_tok,
                            "total_tokens": p_tok + c_tok,
                        },
                        "session": updated_sess.to_summary(),
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as exc:
                    err_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": body.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                        "error": {
                            "type": "upstream_stream_error",
                            "message": f"Upstream SSE stream failed: {type(exc).__name__}",
                        },
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                    yield "data: [DONE]\n\n"

            sse_headers = {
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-AATM-Session-Id": session.session_id,
            }
            if session.account_id:
                sse_headers["X-AATM-Account-Id"] = session.account_id
            return StreamingResponse(upstream_sse_events(), media_type="text/event-stream", headers=sse_headers)

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

        usage_payload = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        session_summary = updated.to_summary()
        bridge_mode = "upstream_proxy" if upstream_url else "diagnostic_only"
        is_diagnostic_only = not bool(upstream_url)

        if body.stream:
            async def sse_events() -> AsyncIterator[str]:
                role_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": body.model,
                    "mode": bridge_mode,
                    "diagnostic_only": is_diagnostic_only,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                    "session": session_summary,
                }
                yield f"data: {json.dumps(role_chunk)}\n\n"

                for piece in _split_sse_chunks(content):
                    delta_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": body.model,
                        "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(delta_chunk)}\n\n"

                final_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": body.model,
                    "mode": bridge_mode,
                    "diagnostic_only": is_diagnostic_only,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": usage_payload,
                    "session": session_summary,
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            sse_headers = {
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-AATM-Session-Id": updated.session_id,
            }
            if updated.account_id:
                sse_headers["X-AATM-Account-Id"] = updated.account_id
            return StreamingResponse(sse_events(), media_type="text/event-stream", headers=sse_headers)

        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created_ts,
            "model": body.model,
            "mode": bridge_mode,
            "diagnostic_only": is_diagnostic_only,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage_payload,
            "session": session_summary,
        }

    @bridge_app.get("/v1/sessions", dependencies=[Depends(_verify_bearer_auth)])
    async def list_sessions(
        x_aatm_account_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        account_filter = (x_aatm_account_id or "").strip() or None
        sessions = await store.list_summaries(account_id=account_filter)
        metrics = await store.metrics(account_id=account_filter)
        metrics["uptime_seconds"] = round(time.monotonic() - started_at, 2)
        return {
            "status": "ok",
            "service": "aatm-arena-local-bridge",
            "version": BRIDGE_VERSION,
            "count": len(sessions),
            "active_sessions": len(sessions),
            "account_scope": account_filter,
            "metrics": metrics,
            "sessions": sessions,
        }

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
