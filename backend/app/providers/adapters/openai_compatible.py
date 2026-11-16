"""OpenAI-compatible / Ollama adapter."""

import time

import httpx

from app.providers.adapters.common import (
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
    shared_ssl_context,
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

