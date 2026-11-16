import asyncio
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.infra import _serialize_provider
from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.models import AIProvider, ProviderUsageLog
from app.providers.adapters import (
    DEFAULT_BRIDGE_MAX_CONCURRENCY,
    DEFAULT_BRIDGE_TIMEOUT_SECONDS,
    ArenaAgentAdapter,
    ArenaLocalBridgeAdapter,
    _bridge_max_concurrency,
    _bridge_timeout_seconds,
)
from app.providers.base import ProviderRequest, ProviderResponse, validate_credential_ref
from app.providers.router import ProviderRouter


@dataclass
class ScriptedReply:
    status_code: int = 200
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


class FastAPIBridgeServer:
    """A local FastAPI bridge fake with real HTTP sockets and scripted responses."""

    def __init__(
        self,
        *,
        expected_bearer: str | None = None,
        expected_api_key: str | None = None,
        ready_status: int = 200,
        health_status: int = 200,
        chat_replies: list[ScriptedReply] | None = None,
        completion_delay_s: float = 0.0,
    ) -> None:
        self.expected_bearer = expected_bearer
        self.expected_api_key = expected_api_key
        self.ready_status = ready_status
        self.health_status = health_status
        self.chat_replies: deque[ScriptedReply] = deque(chat_replies or [])
        self.completion_delay_s = completion_delay_s
        self.chat_requests: list[dict[str, Any]] = []
        self.chat_request_times: list[float] = []
        self.active_completions = 0
        self.max_active_completions = 0
        self.anthropic_requests: list[dict[str, Any]] = []
        self.checked_paths: list[str] = []
        self.bearer_checks: list[bool] = []
        self.api_key_checks: list[bool] = []
        self.sessions: dict[str, str] = {}
        self.base_url = ""
        self._socket: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def _valid_bearer(self, request: Request) -> bool:
        if self.expected_bearer is None:
            return True
        valid = request.headers.get("authorization") == f"Bearer {self.expected_bearer}"
        self.bearer_checks.append(valid)
        return valid

    def _valid_api_key(self, request: Request) -> bool:
        if self.expected_api_key is None:
            return True
        valid = request.headers.get("x-api-key") == self.expected_api_key
        self.api_key_checks.append(valid)
        return valid

    def __enter__(self) -> "FastAPIBridgeServer":
        app = FastAPI()

        @app.get("/ready")
        async def ready(request: Request):
            self.checked_paths.append("/ready")
            if not self._valid_bearer(request):
                raise HTTPException(status_code=401, detail="invalid bearer token")
            if self.ready_status == 404:
                return JSONResponse({"detail": "not found"}, status_code=404)
            return JSONResponse(
                {"status": "ready" if self.ready_status < 400 else "not_ready", "detail": "account not ready"},
                status_code=self.ready_status,
            )

        @app.get("/health")
        async def health(request: Request):
            self.checked_paths.append("/health")
            if not self._valid_bearer(request):
                raise HTTPException(status_code=401, detail="invalid bearer token")
            return JSONResponse({"status": "ok"}, status_code=self.health_status)

        @app.post("/v1/chat/completions")
        async def chat_completions(request: Request):
            payload = await request.json()
            self.chat_requests.append({"headers": dict(request.headers), "payload": payload})
            self.chat_request_times.append(time.monotonic())
            if not self._valid_bearer(request):
                raise HTTPException(status_code=401, detail="invalid bearer token")
            self.active_completions += 1
            self.max_active_completions = max(self.max_active_completions, self.active_completions)
            try:
                if self.completion_delay_s:
                    await asyncio.sleep(self.completion_delay_s)
                if self.chat_replies:
                    reply = self.chat_replies.popleft()
                    if reply.status_code != 200:
                        return JSONResponse(
                            reply.body or {"error": "scripted bridge error"},
                            status_code=reply.status_code,
                            headers=reply.headers,
                        )
                    if reply.body is not None:
                        return JSONResponse(reply.body, status_code=reply.status_code, headers=reply.headers)
                return JSONResponse(
                    {
                        "id": "chatcmpl-test",
                        "model": payload.get("model", "agent"),
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "FastAPI bridge reply"}}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    }
                )
            finally:
                self.active_completions -= 1

        @app.post("/v1/messages")
        async def anthropic_messages(request: Request):
            payload = await request.json()
            self.anthropic_requests.append({"headers": dict(request.headers), "payload": payload})
            if not self._valid_api_key(request):
                raise HTTPException(status_code=401, detail="invalid API key")
            return JSONResponse(
                {
                    "id": "msg-test",
                    "type": "message",
                    "model": payload.get("model", "claude-sonnet-4-5"),
                    "content": [{"type": "text", "text": "Anthropic failover reply"}],
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                }
            )

        @app.get("/v1/sessions/{session_id}")
        async def get_session(session_id: str, request: Request):
            if not self._valid_bearer(request):
                raise HTTPException(status_code=401, detail="invalid bearer token")
            if session_id not in self.sessions:
                raise HTTPException(status_code=404, detail="session not found")
            account_id = request.headers.get("x-aatm-account-id")
            if account_id and self.sessions[session_id] != account_id:
                raise HTTPException(status_code=403, detail="cross-account session access denied")
            return {"session_id": session_id, "account_id": self.sessions[session_id]}

        @app.delete("/v1/sessions/{session_id}")
        async def delete_session(session_id: str, request: Request):
            if not self._valid_bearer(request):
                raise HTTPException(status_code=401, detail="invalid bearer token")
            if session_id not in self.sessions:
                raise HTTPException(status_code=404, detail="session not found")
            account_id = request.headers.get("x-aatm-account-id")
            if account_id and self.sessions[session_id] != account_id:
                raise HTTPException(status_code=403, detail="cross-account session access denied")
            del self.sessions[session_id]
            return {"deleted": True, "session_id": session_id}

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(128)
        self._socket.setblocking(False)
        port = self._socket.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

        config = uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [self._socket]},
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while not self._server.started and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self._server.started:
            self.__exit__(None, None, None)
            raise RuntimeError("FastAPI bridge test server did not start")
        return self

    def __exit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._socket is not None:
            self._socket.close()


@pytest.fixture
async def db_session() -> AsyncSession:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        yield db


async def _enable_only(db: AsyncSession, *provider_ids: str) -> None:
    result = await db.execute(select(AIProvider))
    enabled = set(provider_ids)
    for provider in result.scalars():
        provider.is_enabled = provider.id in enabled
    await db.commit()


async def test_missing_credentials_are_skipped_and_reported_not_configured(db_session, monkeypatch):
    missing_refs = {
        "arena-local-bridge": "PHASE2_MISSING_BRIDGE_TOKEN",
        "arena-agent": "PHASE2_MISSING_AGENT_TOKEN",
        "anthropic-claude": "PHASE2_MISSING_ANTHROPIC_KEY",
    }
    for env_name in missing_refs.values():
        monkeypatch.delenv(env_name, raising=False)
        assert validate_credential_ref(f"env:{env_name}")[0] is False

    assert validate_credential_ref(None)[0] is False
    monkeypatch.setenv("PHASE2_MISSING_BRIDGE_TOKEN", "   ")
    assert validate_credential_ref("env:PHASE2_MISSING_BRIDGE_TOKEN")[0] is False
    monkeypatch.setenv("PHASE2_MISSING_BRIDGE_TOKEN", "configured-test-token")
    assert validate_credential_ref("env:PHASE2_MISSING_BRIDGE_TOKEN")[0] is True
    monkeypatch.delenv("PHASE2_MISSING_BRIDGE_TOKEN")

    for provider_id, env_name in missing_refs.items():
        provider = await db_session.get(AIProvider, provider_id)
        assert provider is not None
        provider.credential_ref = f"env:{env_name}"
        provider.endpoint_url = "http://127.0.0.1:1"
        provider.is_enabled = True
    bridge = await db_session.get(AIProvider, "arena-local-bridge")
    assert bridge is not None
    bridge.health_status = "healthy"
    await db_session.commit()

    serialized = _serialize_provider(bridge)
    assert serialized["credential_status"] == "not_configured"
    assert serialized["health_status"] == "degraded"

    response = await ProviderRouter().route_and_execute(db_session, ProviderRequest(prompt="must not be sent"))
    assert response.success is False
    assert response.content == ""
    assert response.estimated_cost_usd == 0.0
    assert "not configured" in (response.error_message or "").lower()
    logs = list((await db_session.execute(select(ProviderUsageLog))).scalars())
    assert logs == []


async def test_seeded_bridge_defaults_are_local_and_openai_compatible(db_session):
    provider = await db_session.get(AIProvider, "arena-local-bridge")
    assert provider is not None
    assert provider.endpoint_url == get_settings().ARENA_BRIDGE_URL
    assert provider.default_model == "agent"
    assert provider.available_models == ["agent", "ping"]


def test_bridge_timeout_and_concurrency_defaults(monkeypatch):
    settings = get_settings()
    monkeypatch.delenv("ARENA_BRIDGE_TIMEOUT", raising=False)
    monkeypatch.delenv("ARENA_BRIDGE_MAX_CONCURRENCY", raising=False)
    monkeypatch.setattr(settings, "ARENA_BRIDGE_TIMEOUT", DEFAULT_BRIDGE_TIMEOUT_SECONDS)
    monkeypatch.setattr(settings, "ARENA_BRIDGE_MAX_CONCURRENCY", DEFAULT_BRIDGE_MAX_CONCURRENCY)

    assert _bridge_timeout_seconds() == 600.0
    assert _bridge_max_concurrency() == 1


async def test_bridge_concurrency_limit_uses_module_semaphore(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "fastapi-bridge-concurrency-token")
    monkeypatch.setenv("ARENA_BRIDGE_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("ARENA_BRIDGE_TIMEOUT", "2")
    with FastAPIBridgeServer(
        expected_bearer="fastapi-bridge-concurrency-token", completion_delay_s=0.04
    ) as server:
        adapter = ArenaLocalBridgeAdapter(
            "bridge-concurrency", server.base_url, "env:ARENA_BRIDGE_TOKEN", "agent"
        )
        responses = await asyncio.gather(
            adapter.generate(ProviderRequest(prompt="concurrent one")),
            adapter.generate(ProviderRequest(prompt="concurrent two")),
        )

    assert all(response.success for response in responses)
    assert server.max_active_completions == 1


async def test_arena_agent_never_answers_or_reports_healthy_while_the_connector_is_off(db_session, monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ARENA_CONNECTOR_ENABLED", False)
    monkeypatch.setattr(settings, "ARENA_MOCK_URL", "")
    monkeypatch.setenv("ARENA_SESSION_TOKEN", "configured-test-session-token")
    await _enable_only(db_session, "arena-agent")

    adapter = ArenaAgentAdapter(
        "arena-agent",
        "https://arena.ai/agent",
        "env:ARENA_SESSION_TOKEN",
        "arena-agent-orchestrator",
    )
    health = await adapter.health_check()
    assert health.status != "healthy"
    assert health.detail.startswith("not_enabled")

    prompt = "echo-stub-must-not-return-this"
    direct = await adapter.generate(ProviderRequest(prompt=prompt, metadata={"browser_profile_id": "profile-qa-e2e"}))
    assert direct.success is False
    assert direct.metadata["code"] == "not_enabled"
    assert direct.content == ""
    assert direct.estimated_cost_usd == 0.0

    routed = await ProviderRouter().route_and_execute(
        db_session,
        ProviderRequest(prompt=prompt),
        preferred_provider_id="arena-agent",
    )
    assert routed.success is False
    assert routed.provider_id == "none"
    assert "Arena connector is not enabled" in (routed.error_message or "")
    assert routed.content == ""
    assert routed.estimated_cost_usd == 0.0
    provider = await db_session.get(AIProvider, "arena-agent")
    assert provider is not None
    assert provider.health_status == "degraded"
    logs = list((await db_session.execute(select(ProviderUsageLog))).scalars())
    assert len(logs) == 1
    assert logs[0].success is False
    assert logs[0].estimated_cost_usd == 0.0


async def test_bridge_health_ready_not_ready_and_health_fallback(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "fastapi-bridge-health-token")

    with FastAPIBridgeServer(expected_bearer="fastapi-bridge-health-token", ready_status=200) as server:
        adapter = ArenaLocalBridgeAdapter(
            "bridge-health", server.base_url, "env:ARENA_BRIDGE_TOKEN", "agent"
        )
        health = await adapter.health_check()
        assert health.status == "healthy"
        assert health.credentials_configured is True
        assert server.checked_paths == ["/ready"]
        assert server.bearer_checks == [True]

    with FastAPIBridgeServer(expected_bearer="fastapi-bridge-health-token", ready_status=503) as server:
        adapter = ArenaLocalBridgeAdapter(
            "bridge-not-ready", server.base_url, "env:ARENA_BRIDGE_TOKEN", "agent"
        )
        health = await adapter.health_check()
        assert health.status == "degraded"
        assert "account not ready" in health.detail.lower()
        assert server.checked_paths == ["/ready"]
        assert server.bearer_checks == [True]

    with FastAPIBridgeServer(
        expected_bearer="fastapi-bridge-health-token", ready_status=404, health_status=200
    ) as server:
        adapter = ArenaLocalBridgeAdapter(
            "bridge-health-fallback", server.base_url, "env:ARENA_BRIDGE_TOKEN", "agent"
        )
        health = await adapter.health_check()
        assert health.status == "healthy"
        assert server.checked_paths == ["/ready", "/health"]
        assert server.bearer_checks == [True, True]


async def test_bridge_uses_a_persistent_session_header_per_employee(db_session, monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "fastapi-bridge-session-token")
    await _enable_only(db_session, "arena-local-bridge")
    bridge = await db_session.get(AIProvider, "arena-local-bridge")
    assert bridge is not None

    with FastAPIBridgeServer(expected_bearer="fastapi-bridge-session-token") as server:
        bridge.endpoint_url = server.base_url
        await db_session.commit()
        router = ProviderRouter()
        results = []
        for agent_id in ("worker-1", "worker-2", "worker-1"):
            results.append(
                await router.route_and_execute(
                    db_session,
                    ProviderRequest(prompt=f"request for {agent_id}", agent_id=agent_id),
                    preferred_provider_id=bridge.id,
                )
            )

        assert all(result.success for result in results)
        assert len(server.chat_requests) == 3
        headers = [record["headers"] for record in server.chat_requests]
        codex_session_ids = [item["x-codex-session-id"] for item in headers]
        aatm_session_ids = [item["x-aatm-session-id"] for item in headers]
        assert codex_session_ids == ["sess-worker-1", "sess-worker-2", "sess-worker-1"]
        assert aatm_session_ids == codex_session_ids
        assert all(item["authorization"] == "Bearer fastapi-bridge-session-token" for item in headers)
        assert all(record["payload"]["stream"] is False for record in server.chat_requests)
        assert all(server.bearer_checks)

        logs = list((await db_session.execute(select(ProviderUsageLog))).scalars())
        assert len(logs) == 3
        assert all(log.input_tokens == 11 and log.output_tokens == 7 for log in logs)
        assert all(log.input_tokens_estimated is False and log.output_tokens_estimated is False for log in logs)


async def test_missing_usage_is_estimated_and_flagged_in_logs(db_session, monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "fastapi-bridge-estimate-token")
    await _enable_only(db_session, "arena-local-bridge")
    bridge = await db_session.get(AIProvider, "arena-local-bridge")
    assert bridge is not None
    body = {
        "id": "chatcmpl-estimated",
        "model": "agent",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "estimated usage reply"}}],
    }

    with FastAPIBridgeServer(
        expected_bearer="fastapi-bridge-estimate-token", chat_replies=[ScriptedReply(body=body)]
    ) as server:
        bridge.endpoint_url = server.base_url
        await db_session.commit()
        response = await ProviderRouter().route_and_execute(
            db_session,
            ProviderRequest(prompt="estimate the missing usage"),
            preferred_provider_id=bridge.id,
        )

    assert response.success is True
    assert response.input_tokens > 0 and response.output_tokens > 0
    assert response.input_tokens_estimated is True
    assert response.output_tokens_estimated is True
    log = (await db_session.execute(select(ProviderUsageLog))).scalar_one()
    assert log.input_tokens_estimated is True
    assert log.output_tokens_estimated is True


async def test_bridge_timeout_is_a_failed_zero_cost_call(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "fastapi-bridge-timeout-token")
    monkeypatch.setenv("ARENA_BRIDGE_TIMEOUT", "0.05")

    with FastAPIBridgeServer(
        expected_bearer="fastapi-bridge-timeout-token", completion_delay_s=0.2
    ) as server:
        adapter = ArenaLocalBridgeAdapter(
            "bridge-timeout", server.base_url, "env:ARENA_BRIDGE_TOKEN", "agent"
        )
        response = await adapter.generate(ProviderRequest(prompt="wait for timeout"))

    assert response.success is False
    assert response.content == ""
    assert response.estimated_cost_usd == 0.0
    assert response.metadata["bridge_offline"] is True
    assert response.input_tokens_estimated is True
    assert len(server.chat_requests) == 1


async def test_retry_after_is_capped_and_the_same_provider_is_retried(db_session, monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "fastapi-bridge-retry-token")
    monkeypatch.setenv("PROVIDER_MAX_RETRIES", "1")
    monkeypatch.setenv("PROVIDER_RETRY_AFTER_MAX_SECONDS", "0.02")
    await _enable_only(db_session, "arena-local-bridge")
    bridge = await db_session.get(AIProvider, "arena-local-bridge")
    assert bridge is not None

    assert ProviderRouter._retry_delay(
        ProviderResponse(
            provider_id=bridge.id,
            model="agent",
            content="",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=0.0,
            retryable=True,
            retry_after_s=5.0,
        )
    ) == pytest.approx(0.02)

    with FastAPIBridgeServer(
        expected_bearer="fastapi-bridge-retry-token",
        chat_replies=[
            ScriptedReply(429, {"error": "rate limited"}, {"Retry-After": "0.25"}),
            ScriptedReply(),
        ],
    ) as server:
        bridge.endpoint_url = server.base_url
        await db_session.commit()
        started = time.monotonic()
        response = await ProviderRouter().route_and_execute(
            db_session,
            ProviderRequest(prompt="retry me"),
            preferred_provider_id=bridge.id,
        )
        elapsed = time.monotonic() - started

        assert response.success is True
        assert response.provider_id == bridge.id
        assert len(server.chat_requests) == 2
        assert server.chat_request_times[1] - server.chat_request_times[0] >= 0.015
        assert elapsed < 0.2
        assert all(record["headers"]["authorization"] == "Bearer fastapi-bridge-retry-token" for record in server.chat_requests)

    logs = list((await db_session.execute(select(ProviderUsageLog))).scalars())
    assert len(logs) == 2
    assert logs[0].success is False
    assert logs[0].estimated_cost_usd == 0.0
    assert logs[0].input_tokens_estimated is True
    assert logs[1].success is True
    assert logs[1].input_tokens == 11 and logs[1].output_tokens == 7
    assert logs[1].input_tokens_estimated is False and logs[1].output_tokens_estimated is False


async def test_exhausted_bridge_retries_fail_over_to_configured_anthropic(db_session, monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "fastapi-bridge-failover-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fastapi-anthropic-failover-key")
    monkeypatch.delenv("ARENA_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("PROVIDER_MAX_RETRIES", "1")
    await _enable_only(db_session, "arena-local-bridge", "anthropic-claude")
    bridge = await db_session.get(AIProvider, "arena-local-bridge")
    anthropic = await db_session.get(AIProvider, "anthropic-claude")
    assert bridge is not None and anthropic is not None

    with FastAPIBridgeServer(
        expected_bearer="fastapi-bridge-failover-token",
        expected_api_key="fastapi-anthropic-failover-key",
        chat_replies=[
            ScriptedReply(503, {"error": "account not ready"}, {"Retry-After": "0"}),
            ScriptedReply(429, {"error": "rate limited"}, {"Retry-After": "0"}),
        ],
    ) as server:
        bridge.endpoint_url = server.base_url
        anthropic.endpoint_url = f"{server.base_url}/v1"
        await db_session.commit()

        response = await ProviderRouter().route_and_execute(
            db_session,
            ProviderRequest(prompt="fail over safely", agent_id="worker-1"),
            preferred_provider_id=bridge.id,
        )

        assert response.success is True
        assert response.provider_id == anthropic.id
        assert response.content == "Anthropic failover reply"
        assert len(server.chat_requests) == 2
        assert all(record["payload"]["stream"] is False for record in server.chat_requests)
        assert len(server.anthropic_requests) == 1
        assert server.api_key_checks == [True]

    logs = list((await db_session.execute(select(ProviderUsageLog).order_by(ProviderUsageLog.created_at))).scalars())
    bridge_logs = [log for log in logs if log.provider_id == bridge.id]
    anthropic_logs = [log for log in logs if log.provider_id == anthropic.id]
    assert len(bridge_logs) == 2
    assert all(log.success is False and log.estimated_cost_usd == 0.0 for log in bridge_logs)
    assert all(log.input_tokens_estimated is True for log in bridge_logs)
    assert len(anthropic_logs) == 1
    assert anthropic_logs[0].success is True
    assert anthropic_logs[0].input_tokens == 20 and anthropic_logs[0].output_tokens == 10
    assert anthropic_logs[0].input_tokens_estimated is False
    assert anthropic_logs[0].output_tokens_estimated is False
