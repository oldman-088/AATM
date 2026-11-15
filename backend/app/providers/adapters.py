import time

import httpx

from app.core.security import resolve_credential_ref
from app.providers.base import (
    BaseProviderAdapter,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
)

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


class ArenaLocalBridgeAdapter(BaseProviderAdapter):
    async def health_check(self) -> ProviderHealth:
        t0 = time.perf_counter()
        url = (self.endpoint_url or "http://127.0.0.1:4100").rstrip("/") + "/health"
        token = resolve_credential_ref(self.credential_ref)
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(url)
                ms = round((time.perf_counter() - t0) * 1000, 2)
                if resp.status_code < 400:
                    return ProviderHealth(self.provider_id, "healthy", ms, f"Bridge reachable ({resp.status_code})", bool(token))
                return ProviderHealth(self.provider_id, "degraded", ms, f"Bridge HTTP {resp.status_code}", bool(token))
        except Exception as exc:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProviderHealth(
                self.provider_id,
                "offline",
                ms,
                f"Arena Local Bridge not running at {self.endpoint_url}: {type(exc).__name__}",
                bool(token),
            )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        t0 = time.perf_counter()
        model = req.model or self.default_model
        in_tok = self.estimate_tokens(req.system_prompt + req.prompt)
        if req.dry_run:
            out_tok = 64
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content="[dry-run] Arena Local Bridge execution preview.",
                input_tokens=in_tok,
                output_tokens=out_tok,
                estimated_cost_usd=compute_cost(model, in_tok, out_tok),
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )

        url = (self.endpoint_url or "http://127.0.0.1:4100").rstrip("/") + "/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        token = resolve_credential_ref(self.credential_ref)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": req.system_prompt},
                            {"role": "user", "content": req.prompt},
                        ],
                        "max_tokens": req.max_tokens,
                        "temperature": req.temperature,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                out_tok = self.estimate_tokens(text)
                return ProviderResponse(
                    provider_id=self.provider_id,
                    model=model,
                    content=text,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    estimated_cost_usd=compute_cost(model, in_tok, out_tok),
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
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
                error_message=f"Arena Local Bridge unavailable: {exc}",
            )


class ArenaAgentAdapter(BaseProviderAdapter):
    async def health_check(self) -> ProviderHealth:
        token = resolve_credential_ref(self.credential_ref)
        return ProviderHealth(
            provider_id=self.provider_id,
            status="healthy",
            latency_ms=4.5,
            detail="Arena Agent internal runtime ready (session-managed)",
            credentials_configured=bool(token),
        )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        t0 = time.perf_counter()
        model = req.model or self.default_model
        in_tok = self.estimate_tokens(req.system_prompt + req.prompt)
        content = (
            f"[Arena Agent Runtime | {model}] Processed directive: {req.prompt[:180]}"
            + ("..." if len(req.prompt) > 180 else "")
        )
        out_tok = self.estimate_tokens(content)
        return ProviderResponse(
            provider_id=self.provider_id,
            model=model,
            content=content,
            input_tokens=in_tok,
            output_tokens=out_tok,
            estimated_cost_usd=compute_cost(model, in_tok, out_tok),
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            metadata={"runtime": "arena_agent"},
        )


class AnthropicClaudeAdapter(BaseProviderAdapter):
    async def health_check(self) -> ProviderHealth:
        api_key = resolve_credential_ref(self.credential_ref)
        if not api_key:
            return ProviderHealth(
                self.provider_id,
                "degraded",
                1.0,
                f"Credential {self.credential_ref} not set in environment; live API calls disabled (safe zero-spend mode).",
                credentials_configured=False,
            )
        return ProviderHealth(
            self.provider_id,
            "healthy",
            12.0,
            "Anthropic API credential reference resolved and ready.",
            credentials_configured=True,
        )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        t0 = time.perf_counter()
        model = req.model or self.default_model
        in_tok = self.estimate_tokens(req.system_prompt + req.prompt)
        api_key = resolve_credential_ref(self.credential_ref)

        if req.dry_run or not api_key:
            reason = "dry-run" if req.dry_run else f"missing {self.credential_ref}"
            content = f"[Anthropic {model} | {reason}] Safe estimate: ~{in_tok} input tokens. Configure {self.credential_ref} to enable live Anthropic calls."
            out_tok = self.estimate_tokens(content)
            return ProviderResponse(
                provider_id=self.provider_id,
                model=model,
                content=content,
                input_tokens=in_tok,
                output_tokens=out_tok,
                estimated_cost_usd=compute_cost(model, in_tok, out_tok) if req.dry_run else 0.0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                success=True,
                metadata={"mode": reason},
            )

        url = (self.endpoint_url or "https://api.anthropic.com/v1").rstrip("/") + "/messages"
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
                usage = data.get("usage", {})
                real_in = int(usage.get("input_tokens", in_tok))
                real_out = int(usage.get("output_tokens", 128))
                blocks = data.get("content", [])
                text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
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
                error_message=f"Anthropic API error: {exc}",
            )


def build_adapter(provider_type: str, provider_id: str, endpoint_url: str | None, credential_ref: str | None, default_model: str) -> BaseProviderAdapter:
    if provider_type == "arena_local_bridge":
        return ArenaLocalBridgeAdapter(provider_id, endpoint_url, credential_ref, default_model)
    if provider_type == "anthropic_claude":
        return AnthropicClaudeAdapter(provider_id, endpoint_url, credential_ref, default_model)
    return ArenaAgentAdapter(provider_id, endpoint_url, credential_ref, default_model)
