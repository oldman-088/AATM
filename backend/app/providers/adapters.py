import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.providers.base import (
    BaseProviderAdapter,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
)

DEFAULT_ARENA_BRIDGE_URL = "http://127.0.0.1:4100"
DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1"

MODEL_PRICING_PER_1M = {
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-haiku-4-5": (0.8, 4.0),
    "arena-bridge-default": (0.0, 0.0),
    "arena-reasoning-pro": (1.5, 6.0),
    "arena-fast": (0.2, 0.8),
    "arena-agent-orchestrator": (0.0, 0.0),
    "arena-agent-coder": (0.0, 0.0),
}


def compute_cost(model: str, in_tok: int, out_tok: int) -> float:
    in_rate, out_rate = MODEL_PRICING_PER_1M.get(model, (1.0, 3.0))
    return round((in_tok / 1_000_000) * in_rate + (out_tok / 1_000_000) * out_rate, 6)


def _validate_http_endpoint(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, f"Endpoint URL must use http:// or https:// with a valid host (got scheme='{parsed.scheme or 'none'}')."
    return True, url.rstrip("/")


class ArenaLocalBridgeAdapter(BaseProviderAdapter):
    def _base_url(self) -> str:
        return (self.endpoint_url or DEFAULT_ARENA_BRIDGE_URL).rstrip("/")

    async def health_check(self) -> ProviderHealth:
        t0 = time.perf_counter()
        if not self.credential_ref_valid:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProviderHealth(self.provider_id, "degraded", ms, self.credential_ref_error, credentials_configured=False)

        ok, normalized_or_err = _validate_http_endpoint(self._base_url())
        if not ok:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProviderHealth(self.provider_id, "degraded", ms, normalized_or_err, credentials_configured=False)

        base_url = normalized_or_err
        url = base_url + "/health"
        token = self.resolve_credential()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(url, headers=headers)
                ms = round((time.perf_counter() - t0) * 1000, 2)
                if resp.status_code < 400:
                    return ProviderHealth(
                        self.provider_id,
                        "healthy",
                        ms,
                        f"Bridge reachable at {base_url} (HTTP {resp.status_code})",
                        bool(token),
                    )
                if resp.status_code in (401, 403):
                    return ProviderHealth(
                        self.provider_id,
                        "degraded",
                        ms,
                        f"Bridge rejected authentication (HTTP {resp.status_code}); check {self.safe_credential_label()}",
                        bool(token),
                    )
                return ProviderHealth(self.provider_id, "degraded", ms, f"Bridge HTTP {resp.status_code}", bool(token))
        except Exception as exc:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProviderHealth(
                self.provider_id,
                "offline",
                ms,
                f"Arena Local Bridge not running at {base_url}: {type(exc).__name__}",
                bool(token),
            )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        t0 = time.perf_counter()
        model = req.model or self.default_model
        in_tok = self.estimate_tokens(req.system_prompt + req.prompt)

        if not self.credential_ref_valid:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=self.credential_ref_error,
                metadata={"policy_error": "invalid_credential_ref"},
            )

        ok, normalized_or_err = _validate_http_endpoint(self._base_url())
        if not ok:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=normalized_or_err,
            )

        base_url = normalized_or_err
        req_meta = dict(req.metadata or {})
        session_id = req_meta.get("session_id")
        account_id = req_meta.get("account_id")
        conversation_id = req_meta.get("conversation_id")
        agent_id = req.agent_id or req_meta.get("agent_id")
        project_id = req_meta.get("project_id")

        session_meta: dict[str, Any] = {}
        if session_id:
            session_meta["session_id"] = str(session_id)
        if account_id:
            session_meta["account_id"] = str(account_id)
        if conversation_id:
            session_meta["conversation_id"] = str(conversation_id)
        if agent_id:
            session_meta["agent_id"] = str(agent_id)
        if project_id:
            session_meta["project_id"] = str(project_id)
        if req.task_id:
            session_meta["task_id"] = str(req.task_id)

        if req.dry_run:
            out_tok = 64
            projected = compute_cost(model, in_tok, out_tok)
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content=f"[dry-run] Arena Local Bridge execution preview ({base_url}, model={model}).",
                input_tokens=in_tok,
                output_tokens=out_tok,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                metadata={
                    "dry_run": True,
                    "projected_cost_usd": projected,
                    "endpoint": base_url,
                    **session_meta,
                },
            )

        url = base_url + "/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        token = self.resolve_credential()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session_id:
            headers["X-AATM-Session-Id"] = str(session_id)
        if account_id:
            headers["X-AATM-Account-Id"] = str(account_id)
        if agent_id:
            headers["X-AATM-Agent-Id"] = str(agent_id)
        if conversation_id:
            headers["X-AATM-Conversation-Id"] = str(conversation_id)

        messages: list[dict[str, str]] = []
        if req.system_prompt:
            messages.append({"role": "system", "content": req.system_prompt})
        messages.append({"role": "user", "content": req.prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        if session_id:
            payload["session_id"] = str(session_id)
        if account_id:
            payload["account_id"] = str(account_id)
        if agent_id:
            payload["user"] = str(agent_id)
        if session_meta:
            payload["metadata"] = session_meta

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices")
                text = ""
                if isinstance(choices, list) and choices:
                    first = choices[0] if isinstance(choices[0], dict) else {}
                    msg = first.get("message") if isinstance(first.get("message"), dict) else {}
                    raw_content = msg.get("content", "")
                    if isinstance(raw_content, str):
                        text = raw_content
                    elif isinstance(raw_content, list):
                        text = "\n".join(
                            b.get("text", "") for b in raw_content if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        text = str(raw_content)

                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                real_in = int(usage.get("prompt_tokens", in_tok)) if usage.get("prompt_tokens") is not None else in_tok
                real_out = (
                    int(usage.get("completion_tokens", self.estimate_tokens(text)))
                    if usage.get("completion_tokens") is not None
                    else self.estimate_tokens(text)
                )
                resp_meta: dict[str, Any] = {
                    "endpoint": base_url,
                    "confirmed_usage": bool(usage),
                    **session_meta,
                }
                if isinstance(data.get("session"), dict):
                    resp_meta["bridge_session"] = data["session"]
                return ProviderResponse(
                    provider_id=self.provider_id,
                    model=model,
                    content=text,
                    input_tokens=real_in,
                    output_tokens=real_out,
                    estimated_cost_usd=compute_cost(model, real_in, real_out),
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    metadata=resp_meta,
                )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Arena Local Bridge HTTP error ({status_code}) at {base_url}",
                metadata={"bridge_offline": False, "http_status": status_code, **session_meta},
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Arena Local Bridge offline at {base_url}: {type(exc).__name__}",
                metadata={"bridge_offline": True, **session_meta},
            )
        except Exception as exc:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Arena Local Bridge error at {base_url}: {type(exc).__name__}",
                metadata={"bridge_offline": False, **session_meta},
            )

    async def get_session(self, session_id: str, account_id: str | None = None) -> dict[str, Any] | None:
        """Inspect a per-employee session context from the Arena Local Bridge daemon."""
        if not self.credential_ref_valid:
            return None
        ok, normalized_or_err = _validate_http_endpoint(self._base_url())
        if not ok:
            return None
        headers: dict[str, str] = {}
        token = self.resolve_credential()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if account_id:
            headers["X-AATM-Account-Id"] = str(account_id)
        url = f"{normalized_or_err}/v1/sessions/{session_id}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, dict) else None
        except Exception:
            return None


class ArenaAgentAdapter(BaseProviderAdapter):
    async def health_check(self) -> ProviderHealth:
        t0 = time.perf_counter()
        if not self.credential_ref_valid:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProviderHealth(self.provider_id, "degraded", ms, self.credential_ref_error, credentials_configured=False)

        token = self.resolve_credential()
        ms = round((time.perf_counter() - t0) * 1000, 2)
        return ProviderHealth(
            provider_id=self.provider_id,
            status="healthy",
            latency_ms=ms,
            detail="Arena Agent internal runtime ready (session-managed)",
            credentials_configured=bool(token),
        )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        t0 = time.perf_counter()
        model = req.model or self.default_model
        in_tok = self.estimate_tokens(req.system_prompt + req.prompt)

        if not self.credential_ref_valid:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=self.credential_ref_error,
                metadata={"policy_error": "invalid_credential_ref"},
            )

        content = (
            f"[Arena Agent Runtime | {model}] Processed directive: {req.prompt[:180]}"
            + ("..." if len(req.prompt) > 180 else "")
        )
        out_tok = self.estimate_tokens(content)
        projected = compute_cost(model, in_tok, out_tok)
        return ProviderResponse(
            provider_id=self.provider_id,
            model=model,
            content=content,
            input_tokens=in_tok,
            output_tokens=out_tok,
            estimated_cost_usd=0.0 if req.dry_run else projected,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            metadata={"runtime": "arena_agent", "dry_run": req.dry_run, "projected_cost_usd": projected},
        )


class AnthropicClaudeAdapter(BaseProviderAdapter):
    async def health_check(self) -> ProviderHealth:
        t0 = time.perf_counter()
        if not self.credential_ref_valid:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProviderHealth(self.provider_id, "degraded", ms, self.credential_ref_error, credentials_configured=False)

        ok, normalized_or_err = _validate_http_endpoint(self.endpoint_url or DEFAULT_ANTHROPIC_URL)
        if not ok:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProviderHealth(self.provider_id, "degraded", ms, normalized_or_err, credentials_configured=False)

        api_key = self.resolve_credential()
        ms = round((time.perf_counter() - t0) * 1000, 2)
        if not api_key:
            return ProviderHealth(
                self.provider_id,
                "degraded",
                ms,
                f"Credential {self.safe_credential_label()} not set in environment; live API calls disabled (safe zero-spend mode).",
                credentials_configured=False,
            )
        return ProviderHealth(
            self.provider_id,
            "healthy",
            ms,
            f"Anthropic API credential reference ({self.safe_credential_label()}) resolved and ready (non-billing check).",
            credentials_configured=True,
        )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        t0 = time.perf_counter()
        model = req.model or self.default_model
        in_tok = self.estimate_tokens(req.system_prompt + req.prompt)

        if not self.credential_ref_valid:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=self.credential_ref_error,
                metadata={"policy_error": "invalid_credential_ref"},
            )

        api_key = self.resolve_credential()
        if req.dry_run or not api_key:
            reason = "dry-run" if req.dry_run else f"missing {self.safe_credential_label()}"
            content = (
                f"[Anthropic {model} | {reason}] Safe estimate: ~{in_tok} input tokens. "
                f"Configure {self.safe_credential_label()} to enable live Anthropic calls."
            )
            out_tok = self.estimate_tokens(content)
            projected = compute_cost(model, in_tok, out_tok)
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content=content,
                input_tokens=in_tok,
                output_tokens=out_tok,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=True,
                metadata={"mode": reason, "dry_run": True, "projected_cost_usd": projected},
            )

        ok, normalized_or_err = _validate_http_endpoint(self.endpoint_url or DEFAULT_ANTHROPIC_URL)
        if not ok:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=normalized_or_err,
            )

        url = normalized_or_err + "/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if req.system_prompt:
            payload["system"] = req.system_prompt

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                real_in = int(usage.get("input_tokens", in_tok))
                real_out = int(usage.get("output_tokens", 128))
                blocks = data.get("content", [])
                text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
                return ProviderResponse(
                    provider_id=self.provider_id,
                    model=model,
                    content=text,
                    input_tokens=real_in,
                    output_tokens=real_out,
                    estimated_cost_usd=compute_cost(model, real_in, real_out),
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    metadata={"confirmed_usage": True},
                )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Anthropic API HTTP error ({status_code})",
                metadata={"http_status": status_code},
            )
        except Exception as exc:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=in_tok,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Anthropic API error: {type(exc).__name__}",
            )


def build_adapter(
    provider_type: str,
    provider_id: str,
    endpoint_url: str | None,
    credential_ref: str | None,
    default_model: str,
) -> BaseProviderAdapter:
    if provider_type == "arena_local_bridge":
        return ArenaLocalBridgeAdapter(provider_id, endpoint_url, credential_ref, default_model)
    if provider_type == "anthropic_claude":
        return AnthropicClaudeAdapter(provider_id, endpoint_url, credential_ref, default_model)
    return ArenaAgentAdapter(provider_id, endpoint_url, credential_ref, default_model)
