import asyncio
import json
import math
import os
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.core.config import get_settings
from app.providers.base import (
    BaseProviderAdapter,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
    ProviderStreamChunk,
    shared_ssl_context,
)

DEFAULT_ARENA_BRIDGE_URL = "http://127.0.0.1:4100"
DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1"
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 600.0
DEFAULT_BRIDGE_MAX_CONCURRENCY = 1
SAFE_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_MODEL_PRICING_PATH = Path(__file__).resolve().with_name("model_pricing.json")


def _load_model_pricing() -> dict[str, tuple[float, float, bool]]:
    try:
        raw = json.loads(_MODEL_PRICING_PATH.read_text(encoding="utf-8"))
        models = raw.get("models") if isinstance(raw, dict) else {}
        table: dict[str, tuple[float, float, bool]] = {}
        if isinstance(models, dict):
            for name, entry in models.items():
                if not isinstance(entry, dict):
                    continue
                in_per_1k = float(entry.get("input_per_1k_usd", 0.0) or 0.0)
                out_per_1k = float(entry.get("output_per_1k_usd", 0.0) or 0.0)
                known = bool(entry.get("pricing_known", False))
                table[str(name)] = (in_per_1k, out_per_1k, known)
        if table:
            return table
    except Exception:
        pass
    return {
        "claude-sonnet-4-5": (0.003, 0.015, True),
        "claude-opus-4-1": (0.015, 0.075, True),
        "claude-haiku-4-5": (0.0008, 0.004, True),
    }


MODEL_PRICING_CATALOG: dict[str, tuple[float, float, bool]] = _load_model_pricing()


def compute_cost_details(model: str, in_tok: int, out_tok: int) -> tuple[float, bool]:
    """Compute estimated USD cost and whether rates are known for the given model."""
    entry = MODEL_PRICING_CATALOG.get(model)
    if entry is None:
        return 0.0, False
    in_per_1k, out_per_1k, known = entry
    if not known:
        return 0.0, False
    cost = round((in_tok / 1_000.0) * in_per_1k + (out_tok / 1_000.0) * out_per_1k, 6)
    return cost, True


def compute_cost(model: str, in_tok: int, out_tok: int) -> float:
    cost, _known = compute_cost_details(model, in_tok, out_tok)
    return cost


def _configured_value(env_name: str, setting_name: str, default: Any) -> Any:
    value = os.environ.get(env_name)
    if value is not None:
        return value
    return getattr(get_settings(), setting_name, default)


def _bridge_timeout_seconds() -> float:
    try:
        value = float(_configured_value("ARENA_BRIDGE_TIMEOUT", "ARENA_BRIDGE_TIMEOUT", DEFAULT_BRIDGE_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_BRIDGE_TIMEOUT_SECONDS
    return value if math.isfinite(value) and value > 0 else DEFAULT_BRIDGE_TIMEOUT_SECONDS


def _bridge_max_concurrency() -> int:
    try:
        value = int(_configured_value("ARENA_BRIDGE_MAX_CONCURRENCY", "ARENA_BRIDGE_MAX_CONCURRENCY", DEFAULT_BRIDGE_MAX_CONCURRENCY))
    except (TypeError, ValueError):
        return DEFAULT_BRIDGE_MAX_CONCURRENCY
    return max(1, value)


_ARENA_BRIDGE_SEMAPHORE_LIMIT = _bridge_max_concurrency()
ARENA_BRIDGE_SEMAPHORE = asyncio.Semaphore(_ARENA_BRIDGE_SEMAPHORE_LIMIT)


_PROFILE_SEMAPHORES: dict[str, tuple[int, asyncio.Semaphore]] = {}


def _bridge_semaphore(profile_key: str | None = None) -> asyncio.Semaphore:
    """Per-profile bridge semaphore (Phase 5/6: one in-flight request per Arena profile/account).

    ``ARENA_BRIDGE_MAX_CONCURRENCY`` is the in-flight limit *per profile*; different employees'
    profiles run in parallel. Requests without an account/agent share the module-level semaphore.
    """
    global ARENA_BRIDGE_SEMAPHORE, _ARENA_BRIDGE_SEMAPHORE_LIMIT
    limit = _bridge_max_concurrency()
    if not profile_key:
        if limit != _ARENA_BRIDGE_SEMAPHORE_LIMIT:
            ARENA_BRIDGE_SEMAPHORE = asyncio.Semaphore(limit)
            _ARENA_BRIDGE_SEMAPHORE_LIMIT = limit
        return ARENA_BRIDGE_SEMAPHORE
    held = _PROFILE_SEMAPHORES.get(profile_key)
    if held is None or held[0] != limit:
        held = (limit, asyncio.Semaphore(limit))
        _PROFILE_SEMAPHORES[profile_key] = held
    return held[1]


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _extract_openai_completion(data: dict[str, Any], input_fallback: int) -> tuple[str, int, int, bool, bool, bool, float | None]:
    choices = data.get("choices")
    text = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            raw_content = message.get("content", "")
            if isinstance(raw_content, str):
                text = raw_content
            elif isinstance(raw_content, list):
                text = "\n".join(
                    block.get("text", "") for block in raw_content if isinstance(block, dict) and block.get("type") == "text"
                )
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    input_raw = usage.get("prompt_tokens")
    output_raw = usage.get("completion_tokens")
    input_estimated = input_raw is None
    output_estimated = output_raw is None
    input_tokens = int(input_raw) if input_raw is not None else input_fallback
    output_tokens = int(output_raw) if output_raw is not None else (BaseProviderAdapter.estimate_tokens(text) if text else 0)
    confirmed_usage = input_raw is not None and output_raw is not None
    raw_cost = usage.get("cost_usd", usage.get("cost"))
    try:
        confirmed_cost = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        confirmed_cost = None
    if confirmed_cost is not None and (not math.isfinite(confirmed_cost) or confirmed_cost < 0):
        confirmed_cost = None
    return text, input_tokens, output_tokens, input_estimated, output_estimated, confirmed_usage, confirmed_cost


def _validate_http_endpoint(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, f"Endpoint URL must use http:// or https:// with a valid host (got scheme='{parsed.scheme or 'none'}')."
    return True, url.rstrip("/")


class ArenaLocalBridgeAdapter(BaseProviderAdapter):
    provider_type = "arena_local_bridge"

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
        token = self.resolve_credential()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=2.0, verify=shared_ssl_context()) as client:
                resp = await client.get(base_url + "/ready", headers=headers)
                checked_path = "/ready"
                if resp.status_code == 404:
                    resp = await client.get(base_url + "/health", headers=headers)
                    checked_path = "/health"
                ms = round((time.perf_counter() - t0) * 1000, 2)
                if 200 <= resp.status_code < 300:
                    return ProviderHealth(
                        self.provider_id,
                        "healthy",
                        ms,
                        f"Bridge readiness confirmed at {base_url}{checked_path} (HTTP {resp.status_code})",
                        True,
                    )
                if resp.status_code in (401, 403):
                    return ProviderHealth(
                        self.provider_id,
                        "degraded",
                        ms,
                        f"Bridge rejected authentication (HTTP {resp.status_code}); check {self.safe_credential_label()}",
                        True,
                    )
                if resp.status_code == 503:
                    return ProviderHealth(self.provider_id, "degraded", ms, "Bridge account not ready (HTTP 503)", True)
                return ProviderHealth(self.provider_id, "degraded", ms, f"Bridge readiness HTTP {resp.status_code}", True)
        except httpx.HTTPError as exc:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProviderHealth(
                self.provider_id,
                "offline",
                ms,
                f"Arena Local Bridge not running at {base_url}: {type(exc).__name__}",
                True,
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
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=self.credential_ref_error,
                metadata={"credential_not_configured": True},
            )

        ok, normalized_or_err = _validate_http_endpoint(self._base_url())
        if not ok:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=0,
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
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message="Dry run: no provider request was sent.",
                metadata={"dry_run": True, "endpoint": base_url, **session_meta},
            )

        url = base_url + "/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        token = self.resolve_credential()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session_id:
            headers["x-codex-session-id"] = str(session_id)
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
            "stream": False,
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
            async with _bridge_semaphore(str(account_id or agent_id or "") or None):
                async with httpx.AsyncClient(timeout=_bridge_timeout_seconds(), verify=shared_ssl_context()) as client:
                    resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid completion response shape")
            text, real_in, real_out, input_estimated, output_estimated, confirmed_usage, confirmed_cost = (
                _extract_openai_completion(data, in_tok)
            )
            resp_meta: dict[str, Any] = {
                "endpoint": base_url,
                "confirmed_usage": confirmed_usage,
                "input_tokens_estimated": input_estimated,
                "output_tokens_estimated": output_estimated,
                **session_meta,
            }
            if isinstance(data.get("session"), dict):
                resp_meta["bridge_session"] = data["session"]
            is_diagnostic_only = bool(data.get("diagnostic_only")) or data.get("mode") == "diagnostic_only"
            if is_diagnostic_only:
                resp_meta["diagnostic_only"] = True
                if not req.allow_diagnostic_fallback:
                    return ProviderResponse(
                        provider_id=self.provider_id,
                        model=str(data.get("model") or model),
                        content="",
                        input_tokens=real_in,
                        output_tokens=real_out,
                        estimated_cost_usd=0.0,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                        success=False,
                        error_message=(
                            "Arena Local Bridge is running in diagnostic_only mode without an upstream model; "
                            "configure a real provider or ARENA_BRIDGE_UPSTREAM_URL."
                        ),
                        metadata=resp_meta,
                        input_tokens_estimated=input_estimated,
                        output_tokens_estimated=output_estimated,
                        pricing_known=False,
                    )
            if not text.strip():
                return ProviderResponse(
                    provider_id=self.provider_id,
                    model=model,
                    content="",
                    input_tokens=real_in,
                    output_tokens=real_out,
                    estimated_cost_usd=0.0,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    success=False,
                    error_message="Arena Local Bridge returned no completion content.",
                    metadata={**resp_meta, "empty_response": True},
                    input_tokens_estimated=input_estimated,
                    output_tokens_estimated=output_estimated,
                )
            resolved_model = str(data.get("model") or model)
            calc_cost, pricing_known = compute_cost_details(resolved_model, real_in, real_out)
            if confirmed_cost is not None:
                final_cost = confirmed_cost
                pricing_known = True
            else:
                final_cost = calc_cost
            return ProviderResponse(
                provider_id=self.provider_id,
                model=resolved_model,
                content=text,
                input_tokens=real_in,
                output_tokens=real_out,
                estimated_cost_usd=final_cost,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                metadata=resp_meta,
                input_tokens_estimated=input_estimated,
                output_tokens_estimated=output_estimated,
                confirmed_cost_usd=confirmed_cost,
                pricing_known=pricing_known,
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            retryable = status_code in (429, 503)
            retry_after = _retry_after_seconds(exc.response.headers.get("Retry-After")) if exc.response is not None else None
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
                metadata={
                    "bridge_offline": status_code in (502, 503, 504),
                    "transport_unavailable": status_code in (502, 503, 504),
                    "http_status": status_code,
                    **session_meta,
                },
                retryable=retryable,
                retry_after_s=retry_after,
                input_tokens_estimated=True,
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
                metadata={"bridge_offline": True, "transport_unavailable": True, **session_meta},
                input_tokens_estimated=True,
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
                input_tokens_estimated=True,
            )

    async def generate_stream(self, req: ProviderRequest) -> AsyncIterator[ProviderStreamChunk]:
        """Consume OpenAI-compatible SSE frames (`stream: true`) from the Arena Local Bridge daemon."""
        t0 = time.perf_counter()
        model = req.model or self.default_model
        in_tok = self.estimate_tokens(req.system_prompt + req.prompt)

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

        if not self.credential_ref_valid:
            yield ProviderStreamChunk(
                provider_id=self.provider_id,
                model=model,
                done=True,
                finish_reason="error",
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=self.credential_ref_error,
                metadata={"credential_not_configured": True, **session_meta},
            )
            return

        ok, normalized_or_err = _validate_http_endpoint(self._base_url())
        if not ok:
            yield ProviderStreamChunk(
                provider_id=self.provider_id,
                model=model,
                done=True,
                finish_reason="error",
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=normalized_or_err,
                metadata={"bridge_offline": False, **session_meta},
            )
            return

        base_url = normalized_or_err
        if req.dry_run:
            yield ProviderStreamChunk(
                provider_id=self.provider_id,
                model=model,
                delta="",
                done=True,
                finish_reason="error",
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message="Dry run: no provider request was sent.",
                metadata={"dry_run": True, "stream": True, "endpoint": base_url, **session_meta},
            )
            return

        url = base_url + "/v1/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        token = self.resolve_credential()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session_id:
            headers["x-codex-session-id"] = str(session_id)
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
            "stream": True,
        }
        if session_id:
            payload["session_id"] = str(session_id)
        if account_id:
            payload["account_id"] = str(account_id)
        if agent_id:
            payload["user"] = str(agent_id)
        if session_meta:
            payload["metadata"] = session_meta

        collected_parts: list[str] = []
        usage_data: dict[str, Any] = {}
        bridge_session: dict[str, Any] | None = None
        finish_reason = "stop"
        stream_error: str | None = None

        try:
            async with _bridge_semaphore(str(account_id or agent_id or "") or None):
                async with httpx.AsyncClient(timeout=_bridge_timeout_seconds(), verify=shared_ssl_context()) as client:
                    async with client.stream("POST", url, headers=headers, json=payload) as resp:
                        if resp.status_code >= 400:
                            status_code = resp.status_code
                            retryable = status_code in (429, 503)
                            retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                            await resp.aread()
                            yield ProviderStreamChunk(
                                provider_id=self.provider_id,
                                model=model,
                                done=True,
                                finish_reason="error",
                                input_tokens=in_tok,
                                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                                success=False,
                                error_message=f"Arena Local Bridge HTTP error ({status_code}) at {base_url}",
                                metadata={
                                    "bridge_offline": status_code in (502, 503, 504),
                                    "transport_unavailable": status_code in (502, 503, 504),
                                    "http_status": status_code,
                                    "stream": True,
                                    **session_meta,
                                },
                                retryable=retryable,
                                retry_after_s=retry_after,
                                input_tokens_estimated=True,
                            )
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

                            if isinstance(frame.get("error"), dict):
                                stream_error = str(frame["error"].get("message") or frame["error"].get("type") or "Upstream stream error")
                            if isinstance(frame.get("usage"), dict):
                                usage_data = frame["usage"]
                            if isinstance(frame.get("session"), dict):
                                bridge_session = frame["session"]

                            choices = frame.get("choices")
                            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                                first = choices[0]
                                if first.get("finish_reason"):
                                    finish_reason = str(first["finish_reason"])
                                delta_obj = first.get("delta")
                                if isinstance(delta_obj, dict):
                                    piece = delta_obj.get("content")
                                    if isinstance(piece, str) and piece:
                                        collected_parts.append(piece)
                                        yield ProviderStreamChunk(
                                            provider_id=self.provider_id,
                                            model=model,
                                            delta=piece,
                                            done=False,
                                            metadata={"stream": True, "endpoint": base_url, **session_meta},
                                        )

            if stream_error is not None or finish_reason == "error":
                yield ProviderStreamChunk(
                    provider_id=self.provider_id,
                    model=model,
                    delta="",
                    done=True,
                    finish_reason="error",
                    input_tokens=in_tok,
                    output_tokens=self.estimate_tokens("".join(collected_parts)) if collected_parts else 0,
                    estimated_cost_usd=0.0,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    success=False,
                    error_message=stream_error or "Arena Local Bridge reported stream error",
                    metadata={"bridge_offline": False, "stream": True, "upstream_stream_error": True, **session_meta},
                    input_tokens_estimated=True,
                    output_tokens_estimated=bool(collected_parts),
                )
                return

            full_text = "".join(collected_parts)
            _, real_in, real_out, input_estimated, output_estimated, confirmed_usage, confirmed_cost = (
                _extract_openai_completion({"usage": usage_data}, in_tok)
            )
            final_meta: dict[str, Any] = {
                "endpoint": base_url,
                "stream": True,
                "confirmed_usage": confirmed_usage,
                "input_tokens_estimated": input_estimated,
                "output_tokens_estimated": output_estimated,
                **session_meta,
            }
            if bridge_session is not None:
                final_meta["bridge_session"] = bridge_session
            if not full_text.strip():
                yield ProviderStreamChunk(
                    provider_id=self.provider_id,
                    model=model,
                    delta="",
                    done=True,
                    finish_reason="error",
                    input_tokens=real_in,
                    output_tokens=real_out,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    success=False,
                    error_message="Arena Local Bridge returned no completion content.",
                    metadata={**final_meta, "empty_response": True},
                    input_tokens_estimated=input_estimated,
                    output_tokens_estimated=output_estimated,
                )
                return
            stream_cost, stream_pricing_known = compute_cost_details(model, real_in, real_out)
            yield ProviderStreamChunk(
                provider_id=self.provider_id,
                model=model,
                delta="",
                done=True,
                finish_reason=finish_reason,
                input_tokens=real_in,
                output_tokens=real_out,
                estimated_cost_usd=confirmed_cost if confirmed_cost is not None else stream_cost,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=True,
                metadata=final_meta,
                input_tokens_estimated=input_estimated,
                output_tokens_estimated=output_estimated,
                confirmed_cost_usd=confirmed_cost,
                pricing_known=True if confirmed_cost is not None else stream_pricing_known,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            yield ProviderStreamChunk(
                provider_id=self.provider_id,
                model=model,
                done=True,
                finish_reason="error",
                input_tokens=in_tok,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Arena Local Bridge offline at {base_url}: {type(exc).__name__}",
                metadata={"bridge_offline": True, "transport_unavailable": True, "stream": True, **session_meta},
                input_tokens_estimated=True,
            )
        except Exception as exc:
            yield ProviderStreamChunk(
                provider_id=self.provider_id,
                model=model,
                done=True,
                finish_reason="error",
                input_tokens=in_tok,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Arena Local Bridge stream error at {base_url}: {type(exc).__name__}",
                metadata={"bridge_offline": False, "stream": True, **session_meta},
                input_tokens_estimated=True,
            )

    async def get_session(self, session_id: str, account_id: str | None = None) -> dict[str, Any] | None:
        """Inspect a per-employee session context from the Arena Local Bridge daemon."""
        cleaned_sid = (session_id or "").strip()
        if not cleaned_sid or not SAFE_SESSION_ID_RE.fullmatch(cleaned_sid):
            return None
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
        url = f"{normalized_or_err}/v1/sessions/{quote(cleaned_sid, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=shared_ssl_context()) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def delete_session(self, session_id: str, account_id: str | None = None) -> dict[str, Any]:
        """Teardown an isolated employee session on the Arena Local Bridge daemon (for employee replacement handoff)."""
        cleaned_sid = (session_id or "").strip()
        if not cleaned_sid or not SAFE_SESSION_ID_RE.fullmatch(cleaned_sid):
            return {
                "deleted": False,
                "session_id": cleaned_sid,
                "account_id": account_id,
                "status": "invalid_session_id",
                "bridge_reachable": False,
                "error": "Invalid session_id format.",
            }
        if not self.credential_ref_valid:
            return {
                "deleted": False,
                "session_id": cleaned_sid,
                "account_id": account_id,
                "status": "policy_error",
                "bridge_reachable": False,
                "error": self.credential_ref_error,
            }
        ok, normalized_or_err = _validate_http_endpoint(self._base_url())
        if not ok:
            return {
                "deleted": False,
                "session_id": cleaned_sid,
                "account_id": account_id,
                "status": "invalid_endpoint",
                "bridge_reachable": False,
                "error": normalized_or_err,
            }

        headers: dict[str, str] = {}
        token = self.resolve_credential()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if account_id:
            headers["X-AATM-Account-Id"] = str(account_id)

        url = f"{normalized_or_err}/v1/sessions/{quote(cleaned_sid, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=shared_ssl_context()) as client:
                resp = await client.delete(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json() if resp.content else {}
                    return {
                        "deleted": bool(data.get("deleted", True)) if isinstance(data, dict) else True,
                        "session_id": cleaned_sid,
                        "account_id": account_id,
                        "status": "deleted",
                        "bridge_reachable": True,
                    }
                if resp.status_code == 404:
                    return {
                        "deleted": False,
                        "session_id": cleaned_sid,
                        "account_id": account_id,
                        "status": "not_found",
                        "bridge_reachable": True,
                    }
                if resp.status_code == 403:
                    return {
                        "deleted": False,
                        "session_id": cleaned_sid,
                        "account_id": account_id,
                        "status": "forbidden",
                        "bridge_reachable": True,
                        "error": "Cross-account session delete denied (HTTP 403).",
                    }
                return {
                    "deleted": False,
                    "session_id": cleaned_sid,
                    "account_id": account_id,
                    "status": "http_error",
                    "http_status": resp.status_code,
                    "bridge_reachable": True,
                    "error": f"Bridge returned HTTP {resp.status_code} on session delete.",
                }
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            return {
                "deleted": False,
                "session_id": cleaned_sid,
                "account_id": account_id,
                "status": "bridge_offline",
                "bridge_reachable": False,
                "error": f"Arena Local Bridge offline at {normalized_or_err}: {type(exc).__name__}",
            }
        except Exception as exc:
            return {
                "deleted": False,
                "session_id": cleaned_sid,
                "account_id": account_id,
                "status": "error",
                "bridge_reachable": False,
                "error": f"Session delete error: {type(exc).__name__}",
            }


class ArenaAgentAdapter(BaseProviderAdapter):
    provider_type = "arena_agent"

    async def health_check(self) -> ProviderHealth:
        t0 = time.perf_counter()
        return ProviderHealth(
            provider_id=self.provider_id,
            status="degraded",
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail="arena_agent runtime is not implemented",
            credentials_configured=bool(self.resolve_credential()),
        )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            model=req.model or self.default_model,
            content="",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=0.0,
            success=False,
            error_message="arena_agent runtime is not implemented",
            metadata={"runtime": "arena_agent", "implemented": False},
        )


class ArenaBrowserAdapter(BaseProviderAdapter):
    """Phase 6: Arena through the employee's own browser profile (userscript <-> local bridge).

    Disabled (``not_enabled``) unless the Arena approval flags + reference are recorded, or a
    loopback ``ARENA_MOCK_URL`` is configured for testing. Never retries automatically: a
    ``needs_human`` stop must be resolved by a person.
    """

    provider_type = "arena_browser"

    def _base_url(self) -> str:
        return (self.endpoint_url or get_settings().ARENA_BRIDGE_URL or "http://127.0.0.1:4100").rstrip("/")

    def _fail(self, req: ProviderRequest, code: str, message: str, t0: float, **meta: Any) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            model="arena_browser",
            content="",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            success=False,
            error_message=message,
            metadata={"runtime": "arena_browser", "code": code, **meta},
            retryable=False,
        )

    async def health_check(self) -> ProviderHealth:
        from app.browser.arena_connector import NOT_ENABLED_MESSAGE, arena_gate, bridge_call

        t0 = time.perf_counter()
        gate = arena_gate()
        if not gate["enabled"]:
            return ProviderHealth(self.provider_id, "degraded", 0.0, f"not_enabled: {NOT_ENABLED_MESSAGE}",
                                  credentials_configured=bool(self.resolve_credential()))
        res = await bridge_call("GET", "/v1/connector/profiles")
        latency = round((time.perf_counter() - t0) * 1000, 2)
        if not res.get("ok"):
            return ProviderHealth(self.provider_id, "offline", latency, f"{res.get('code')}: {res.get('message')}",
                                  credentials_configured=bool(self.resolve_credential()))
        ready = [p for p in res.get("profiles", []) if p.get("status") == "ready"]
        return ProviderHealth(
            self.provider_id,
            "healthy" if ready else "degraded",
            latency,
            f"{gate['mode']} mode, {len(ready)}/{len(res.get('profiles', []))} profiles ready",
            credentials_configured=bool(self.resolve_credential()),
        )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        from app.browser.arena_connector import NOT_ENABLED_MESSAGE, arena_gate

        t0 = time.perf_counter()
        gate = arena_gate()
        if not gate["enabled"]:
            return self._fail(req, "not_enabled", NOT_ENABLED_MESSAGE, t0)
        meta = dict(req.metadata or {})
        profile_id = str(meta.get("browser_profile_id") or "").strip()
        if not profile_id:
            return self._fail(req, "profile_required", "Employee has no browser profile for the Arena connector.", t0)
        if req.dry_run:
            return self._fail(req, "dry_run", "Dry run: no prompt was sent to the browser profile.", t0, dry_run=True)
        token = self.resolve_credential()
        headers = {"Content-Type": "application/json", "X-AATM-Profile-Id": profile_id}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for key, header in (("session_id", "X-AATM-Session-Id"), ("account_id", "X-AATM-Account-Id"), ("agent_id", "X-AATM-Agent-Id")):
            if meta.get(key):
                headers[header] = str(meta[key])
        messages: list[dict[str, str]] = []
        if req.system_prompt:
            messages.append({"role": "system", "content": req.system_prompt})
        messages.append({"role": "user", "content": req.prompt})
        payload = {"model": "arena_browser", "messages": messages, "stream": False,
                   "user": str(meta.get("agent_id") or req.agent_id or ""),
                   "metadata": {"browser_profile_id": profile_id, "task_id": req.task_id}}
        timeout = get_settings().ARENA_REPLY_TIMEOUT_SECONDS + 30
        try:
            async with _bridge_semaphore(profile_id):
                async with httpx.AsyncClient(timeout=timeout, verify=shared_ssl_context()) as client:
                    resp = await client.post(self._base_url() + "/v1/chat/completions", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            return self._fail(req, "bridge_unreachable", f"Local bridge unreachable: {type(exc).__name__}", t0,
                              transport_unavailable=True, browser_profile_id=profile_id)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = None
            detail = detail if isinstance(detail, dict) else {"code": f"http_{resp.status_code}", "message": str(detail or "")[:300]}
            code = str(detail.get("code") or f"http_{resp.status_code}")
            return self._fail(req, code, f"Arena connector: {code}: {detail.get('message', '')}".strip(), t0,
                              browser_profile_id=profile_id, connector_status="needs_human" if code == "needs_human" else None)
        data = resp.json()
        content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        return ProviderResponse(
            provider_id=self.provider_id,
            model="arena_browser",
            content=content,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            estimated_cost_usd=0.0,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            success=bool(content),
            error_message=None if content else "Arena page returned an empty answer.",
            metadata={"runtime": "arena_browser", "mode": gate["mode"], "browser_profile_id": profile_id},
        )


class AnthropicClaudeAdapter(BaseProviderAdapter):
    provider_type = "anthropic_claude"

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
        headers = {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=shared_ssl_context()) as client:
                resp = await client.get(normalized_or_err + "/models", headers=headers)
            ms = round((time.perf_counter() - t0) * 1000, 2)
            if 200 <= resp.status_code < 300:
                return ProviderHealth(
                    self.provider_id,
                    "healthy",
                    ms,
                    f"Anthropic API responded to a non-billing readiness check (HTTP {resp.status_code}).",
                    True,
                )
            if resp.status_code in (401, 403):
                detail = f"Anthropic API rejected authentication (HTTP {resp.status_code}); check {self.safe_credential_label()}"
            else:
                detail = f"Anthropic API readiness HTTP {resp.status_code}"
            return ProviderHealth(self.provider_id, "degraded", ms, detail, True)
        except httpx.HTTPError as exc:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProviderHealth(
                self.provider_id,
                "offline",
                ms,
                f"Anthropic API unavailable: {type(exc).__name__}",
                True,
            )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        t0 = time.perf_counter()
        model = req.model or self.default_model
        input_estimate = self.estimate_tokens(req.system_prompt + req.prompt)

        if not self.credential_ref_valid:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=self.credential_ref_error,
                metadata={"credential_not_configured": True},
            )
        if req.dry_run:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message="Dry run: no provider request was sent.",
                metadata={"dry_run": True},
            )

        api_key = self.resolve_credential()
        ok, normalized_or_err = _validate_http_endpoint(self.endpoint_url or DEFAULT_ANTHROPIC_URL)
        if not ok:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=normalized_or_err,
            )

        url = normalized_or_err + "/messages"
        headers = {
            "x-api-key": api_key or "",
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
            async with httpx.AsyncClient(timeout=30.0, verify=shared_ssl_context()) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid Anthropic response shape")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            input_raw = usage.get("input_tokens")
            output_raw = usage.get("output_tokens")
            input_estimated = input_raw is None
            output_estimated = output_raw is None
            real_in = int(input_raw) if input_raw is not None else input_estimate
            blocks = data.get("content", [])
            text = "\n".join(
                block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
            )
            real_out = int(output_raw) if output_raw is not None else (self.estimate_tokens(text) if text else 0)
            confirmed_usage = input_raw is not None and output_raw is not None
            raw_cost = usage.get("cost_usd", usage.get("cost"))
            try:
                confirmed_cost = float(raw_cost) if raw_cost is not None else None
            except (TypeError, ValueError):
                confirmed_cost = None
            if confirmed_cost is not None and (not math.isfinite(confirmed_cost) or confirmed_cost < 0):
                confirmed_cost = None
            response_metadata = {
                "confirmed_usage": confirmed_usage,
                "input_tokens_estimated": input_estimated,
                "output_tokens_estimated": output_estimated,
            }
            if not text.strip():
                return ProviderResponse(
                    provider_id=self.provider_id,
                    model=model,
                    content="",
                    input_tokens=real_in,
                    output_tokens=real_out,
                    estimated_cost_usd=0.0,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    success=False,
                    error_message="Anthropic API returned no text completion.",
                    metadata={**response_metadata, "empty_response": True},
                    input_tokens_estimated=input_estimated,
                    output_tokens_estimated=output_estimated,
                )
            resolved_model = str(data.get("model") or model)
            calc_cost, pricing_known = compute_cost_details(resolved_model, real_in, real_out)
            return ProviderResponse(
                provider_id=self.provider_id,
                model=resolved_model,
                content=text,
                input_tokens=real_in,
                output_tokens=real_out,
                estimated_cost_usd=confirmed_cost if confirmed_cost is not None else calc_cost,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                metadata=response_metadata,
                input_tokens_estimated=input_estimated,
                output_tokens_estimated=output_estimated,
                confirmed_cost_usd=confirmed_cost,
                pricing_known=True if confirmed_cost is not None else pricing_known,
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            retryable = status_code in (429, 503)
            retry_after = _retry_after_seconds(exc.response.headers.get("Retry-After")) if exc.response is not None else None
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=input_estimate,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Anthropic API HTTP error ({status_code})",
                metadata={
                    "http_status": status_code,
                    "transport_unavailable": status_code in (502, 503, 504),
                },
                retryable=retryable,
                retry_after_s=retry_after,
                input_tokens_estimated=True,
            )
        except httpx.HTTPError as exc:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=input_estimate,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Anthropic API error: {type(exc).__name__}",
                metadata={"transport_unavailable": True},
                input_tokens_estimated=True,
            )
        except Exception as exc:
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="",
                input_tokens=input_estimate,
                output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"Anthropic API error: {type(exc).__name__}",
                input_tokens_estimated=True,
            )


class OpenAICompatibleAdapter(BaseProviderAdapter):
    """Adapter for OpenAI-compatible APIs such as OpenRouter and Ollama."""

    provider_type = "openai_compatible"

    def _base_url(self) -> str:
        return (self.endpoint_url or "").rstrip("/")

    async def health_check(self) -> ProviderHealth:
        t0 = time.perf_counter()
        if not self.credential_ref_valid:
            return ProviderHealth(
                self.provider_id,
                "degraded",
                round((time.perf_counter() - t0) * 1000, 2),
                self.credential_ref_error,
                False,
            )
        ok, normalized_or_err = _validate_http_endpoint(self._base_url())
        if not ok:
            return ProviderHealth(self.provider_id, "degraded", 0.0, normalized_or_err, False)
        token = self.resolve_credential()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=shared_ssl_context()) as client:
                resp = await client.get(normalized_or_err + "/models", headers=headers)
            ms = round((time.perf_counter() - t0) * 1000, 2)
            if 200 <= resp.status_code < 300:
                return ProviderHealth(self.provider_id, "healthy", ms, "OpenAI-compatible models endpoint responded.", True)
            if resp.status_code in (401, 403):
                detail = f"Provider rejected authentication (HTTP {resp.status_code}); check {self.safe_credential_label()}"
            else:
                detail = f"OpenAI-compatible readiness HTTP {resp.status_code}"
            return ProviderHealth(self.provider_id, "degraded", ms, detail, True)
        except httpx.HTTPError as exc:
            return ProviderHealth(
                self.provider_id,
                "offline",
                round((time.perf_counter() - t0) * 1000, 2),
                f"OpenAI-compatible provider unavailable: {type(exc).__name__}",
                True,
            )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        t0 = time.perf_counter()
        model = req.model or self.default_model
        input_estimate = self.estimate_tokens(req.system_prompt + req.prompt)
        if not self.credential_ref_valid:
            return ProviderResponse(
                self.provider_id,
                model,
                "",
                0,
                0,
                0.0,
                round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=self.credential_ref_error,
                metadata={"credential_not_configured": True},
            )
        if req.dry_run:
            return ProviderResponse(
                self.provider_id,
                model,
                "",
                0,
                0,
                0.0,
                round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message="Dry run: no provider request was sent.",
                metadata={"dry_run": True},
            )

        ok, normalized_or_err = _validate_http_endpoint(self._base_url())
        if not ok:
            return ProviderResponse(
                self.provider_id,
                model,
                "",
                0,
                0,
                0.0,
                round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=normalized_or_err,
            )

        req_meta = dict(req.metadata or {})
        session_id = req_meta.get("session_id")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.resolve_credential()}"}
        if session_id:
            headers["X-AATM-Session-Id"] = str(session_id)
        if req.agent_id or req_meta.get("agent_id"):
            headers["X-AATM-Agent-Id"] = str(req.agent_id or req_meta["agent_id"])
        messages = []
        if req.system_prompt:
            messages.append({"role": "system", "content": req.system_prompt})
        messages.append({"role": "user", "content": req.prompt})
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=120.0, verify=shared_ssl_context()) as client:
                resp = await client.post(normalized_or_err + "/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid OpenAI-compatible response shape")
            text, input_tokens, output_tokens, input_estimated, output_estimated, confirmed_usage, confirmed_cost = (
                _extract_openai_completion(data, input_estimate)
            )
            metadata = {
                "confirmed_usage": confirmed_usage,
                "input_tokens_estimated": input_estimated,
                "output_tokens_estimated": output_estimated,
                **{key: value for key, value in req_meta.items() if key in ("session_id", "account_id", "conversation_id")},
            }
            if not text.strip():
                return ProviderResponse(
                    self.provider_id,
                    model,
                    "",
                    input_tokens,
                    output_tokens,
                    0.0,
                    round((time.perf_counter() - t0) * 1000, 2),
                    success=False,
                    error_message="OpenAI-compatible provider returned no completion content.",
                    metadata={**metadata, "empty_response": True},
                    input_tokens_estimated=input_estimated,
                    output_tokens_estimated=output_estimated,
                )
            resolved_model = str(data.get("model") or model)
            calc_cost, pricing_known = compute_cost_details(resolved_model, input_tokens, output_tokens)
            return ProviderResponse(
                self.provider_id,
                resolved_model,
                text,
                input_tokens,
                output_tokens,
                confirmed_cost if confirmed_cost is not None else calc_cost,
                round((time.perf_counter() - t0) * 1000, 2),
                metadata=metadata,
                input_tokens_estimated=input_estimated,
                output_tokens_estimated=output_estimated,
                confirmed_cost_usd=confirmed_cost,
                pricing_known=True if confirmed_cost is not None else pricing_known,
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            return ProviderResponse(
                self.provider_id,
                model,
                "",
                input_estimate,
                0,
                0.0,
                round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"OpenAI-compatible provider HTTP error ({status_code})",
                metadata={"http_status": status_code, "transport_unavailable": status_code in (502, 503, 504)},
                retryable=status_code in (429, 503),
                retry_after_s=_retry_after_seconds(exc.response.headers.get("Retry-After")) if exc.response is not None else None,
                input_tokens_estimated=True,
            )
        except httpx.HTTPError as exc:
            return ProviderResponse(
                self.provider_id,
                model,
                "",
                input_estimate,
                0,
                0.0,
                round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"OpenAI-compatible provider error: {type(exc).__name__}",
                metadata={"transport_unavailable": True},
                input_tokens_estimated=True,
            )
        except Exception as exc:
            return ProviderResponse(
                self.provider_id,
                model,
                "",
                input_estimate,
                0,
                0.0,
                round((time.perf_counter() - t0) * 1000, 2),
                success=False,
                error_message=f"OpenAI-compatible provider error: {type(exc).__name__}",
                input_tokens_estimated=True,
            )


class UnsupportedProviderAdapter(BaseProviderAdapter):
    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, "degraded", 0.0, "Provider adapter is not registered.", False)

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            model=req.model or self.default_model,
            content="",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=0.0,
            success=False,
            error_message="Provider adapter is not registered.",
            metadata={"unsupported_provider": True},
        )


def build_adapter(
    provider_type: str,
    provider_id: str,
    endpoint_url: str | None,
    credential_ref: str | None,
    default_model: str,
) -> BaseProviderAdapter:
    adapter_types: dict[str, type[BaseProviderAdapter]] = {
        "arena_local_bridge": ArenaLocalBridgeAdapter,
        "arena_agent": ArenaAgentAdapter,
        "arena_browser": ArenaBrowserAdapter,
        "anthropic_claude": AnthropicClaudeAdapter,
        "openai_compatible": OpenAICompatibleAdapter,
    }
    adapter_type = adapter_types.get(provider_type, UnsupportedProviderAdapter)
    return adapter_type(provider_id, endpoint_url, credential_ref, default_model)
