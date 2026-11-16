"""Arena browser connector adapter (Phase 6; real Arena off by default)."""

import time
from typing import Any

import httpx

from app.core.config import get_settings
from app.providers.adapters.common import (
    _bridge_semaphore,
)
from app.providers.base import (
    BaseProviderAdapter,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
    shared_ssl_context,
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

