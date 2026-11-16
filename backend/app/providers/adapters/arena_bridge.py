"""Arena Local Bridge adapter (OpenAI-compatible loopback bridge)."""

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

from app.providers.adapters.common import (
    DEFAULT_ARENA_BRIDGE_URL,
    SAFE_SESSION_ID_RE,
    _bridge_semaphore,
    _bridge_timeout_seconds,
    _extract_openai_completion,
    _retry_after_seconds,
    _validate_http_endpoint,
    compute_cost_details,
)
from app.providers.base import (
    BaseProviderAdapter,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
    ProviderStreamChunk,
    shared_ssl_context,
)


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

