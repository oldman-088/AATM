"""Anthropic Claude Messages API adapter."""

import math
import time

import httpx

from app.providers.adapters.common import (
    DEFAULT_ANTHROPIC_URL,
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

