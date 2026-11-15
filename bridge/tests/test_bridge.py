import asyncio
import json
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
for _p in (str(ROOT_DIR), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.db.base import Base  # noqa: E402
from app.db.seed import seed_initial_data  # noqa: E402
from app.db.session import AsyncSessionLocal, engine  # noqa: E402
from app.models import AIProvider, ProviderSession  # noqa: E402
from app.providers.adapters import ArenaLocalBridgeAdapter  # noqa: E402
from app.providers.base import ProviderRequest  # noqa: E402
from app.providers.router import ProviderRouter  # noqa: E402

from bridge.server import create_app  # noqa: E402


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _LiveBridgeProcess:
    """Runs the real bridge FastAPI app on an ephemeral 127.0.0.1 port for end-to-end TCP tests."""

    def __init__(self) -> None:
        self.port = _free_loopback_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.app = create_app()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_LiveBridgeProcess":
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self._server.started, "Bridge server failed to start in time"
        return self

    def __exit__(self, *args) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)


@pytest.mark.asyncio
async def test_bridge_health_and_bearer_auth(monkeypatch):
    bridge_app = create_app()
    transport = ASGITransport(app=bridge_app)

    # 1. Local dev open mode when ARENA_BRIDGE_TOKEN is unset and REQUIRE_AUTH is false
    monkeypatch.delenv("ARENA_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("ARENA_BRIDGE_REQUIRE_AUTH", raising=False)
    async with AsyncClient(transport=transport, base_url="http://bridge.test") as client:
        r_open = await client.get("/health")
        assert r_open.status_code == 200
        body = r_open.json()
        assert body["status"] == "ok"
        assert body["auth_required"] is False
        assert "arena-bridge-default" in body["models"]

        # 2. Fail closed (503) when ARENA_BRIDGE_REQUIRE_AUTH=true and token unset
        monkeypatch.setenv("ARENA_BRIDGE_REQUIRE_AUTH", "true")
        r_closed = await client.get("/health")
        assert r_closed.status_code == 503

        # 3. Bearer token enforcement (401 missing/wrong, 200 valid)
        monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "test-bridge-bearer-token-2026")
        assert (await client.get("/health")).status_code == 401
        assert (await client.get("/health", headers={"Authorization": "Bearer wrong-token"})).status_code == 401
        assert (await client.get("/v1/sessions/sess-x")).status_code == 401

        auth = {"Authorization": "Bearer test-bridge-bearer-token-2026"}
        r_ok = await client.get("/health", headers=auth)
        assert r_ok.status_code == 200
        assert r_ok.json()["auth_required"] is True


@pytest.mark.asyncio
async def test_bridge_chat_completions_independent_sessions_and_account_isolation(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "bridge-session-token-xyz")
    auth = {"Authorization": "Bearer bridge-session-token-xyz"}
    bridge_app = create_app(max_history=6)
    transport = ASGITransport(app=bridge_app)

    async with AsyncClient(transport=transport, base_url="http://bridge.test") as client:
        # Employee A (account A) sends two turns on sess-emp-a
        r1 = await client.post(
            "/v1/chat/completions",
            headers={**auth, "X-AATM-Session-Id": "sess-emp-a", "X-AATM-Account-Id": "acct-alpha", "X-AATM-Agent-Id": "worker-1"},
            json={"model": "arena-bridge-default", "messages": [{"role": "user", "content": "Alpha turn one"}]},
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["choices"][0]["message"]["role"] == "assistant"
        assert "sess-emp-a" in d1["choices"][0]["message"]["content"]
        assert d1["usage"]["prompt_tokens"] >= 1
        assert d1["usage"]["completion_tokens"] >= 1
        assert d1["session"]["turn_count"] == 1
        assert d1["session"]["account_id"] == "acct-alpha"

        r2 = await client.post(
            "/v1/chat/completions",
            headers={**auth, "X-AATM-Session-Id": "sess-emp-a", "X-AATM-Account-Id": "acct-alpha"},
            json={"model": "arena-bridge-default", "messages": [{"role": "user", "content": "Alpha turn two"}]},
        )
        assert r2.status_code == 200
        assert r2.json()["session"]["turn_count"] == 2

        # Employee B (account B) sends turn on independent sess-emp-b
        r3 = await client.post(
            "/v1/chat/completions",
            headers={**auth, "X-AATM-Session-Id": "sess-emp-b", "X-AATM-Account-Id": "acct-beta", "X-AATM-Agent-Id": "worker-3"},
            json={"model": "arena-fast", "messages": [{"role": "user", "content": "Beta isolated prompt"}]},
        )
        assert r3.status_code == 200
        assert r3.json()["session"]["turn_count"] == 1
        assert r3.json()["session"]["account_id"] == "acct-beta"

        # Inspect both sessions independently via GET /v1/sessions/{session_id}
        s_a = await client.get("/v1/sessions/sess-emp-a", headers={**auth, "X-AATM-Account-Id": "acct-alpha"})
        assert s_a.status_code == 200
        assert s_a.json()["turn_count"] == 2
        assert len(s_a.json()["history"]) == 4  # 2 user + 2 assistant
        assert all("Beta" not in m["content"] for m in s_a.json()["history"])

        s_b = await client.get("/v1/sessions/sess-emp-b", headers={**auth, "X-AATM-Account-Id": "acct-beta"})
        assert s_b.status_code == 200
        assert s_b.json()["turn_count"] == 1
        assert all("Alpha" not in m["content"] for m in s_b.json()["history"])

        # Cross-account isolation guard: acct-beta CANNOT write, read, or delete sess-emp-a
        cross_write = await client.post(
            "/v1/chat/completions",
            headers={**auth, "X-AATM-Session-Id": "sess-emp-a", "X-AATM-Account-Id": "acct-beta"},
            json={"model": "arena-bridge-default", "messages": [{"role": "user", "content": "Hijack attempt"}]},
        )
        assert cross_write.status_code == 403

        cross_read = await client.get("/v1/sessions/sess-emp-a", headers={**auth, "X-AATM-Account-Id": "acct-beta"})
        assert cross_read.status_code == 403

        cross_del = await client.delete("/v1/sessions/sess-emp-a", headers={**auth, "X-AATM-Account-Id": "acct-beta"})
        assert cross_del.status_code == 403

        # List sessions filtered by account_id
        list_beta = await client.get("/v1/sessions", headers={**auth, "X-AATM-Account-Id": "acct-beta"})
        assert list_beta.status_code == 200
        assert list_beta.json()["count"] == 1
        assert list_beta.json()["sessions"][0]["session_id"] == "sess-emp-b"

        # Delete & 404 verification
        del_b = await client.delete("/v1/sessions/sess-emp-b", headers={**auth, "X-AATM-Account-Id": "acct-beta"})
        assert del_b.status_code == 200
        assert del_b.json()["deleted"] is True

        missing = await client.get("/v1/sessions/sess-emp-b", headers=auth)
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_e2e_provider_router_and_adapter_forward_employee_sessions_to_live_bridge(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "e2e-live-bridge-token")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)

        with _LiveBridgeProcess() as srv:
            bridge_prov = await db.get(AIProvider, "arena-local-bridge")
            bridge_prov.endpoint_url = srv.base_url
            await db.commit()

            router = ProviderRouter()
            health = await router.check_provider_health(db, bridge_prov)
            assert health.status == "healthy"
            assert health.credentials_configured is True

            # Worker 1 & Worker 3 execute independently; router resolves each employee's ProviderSession.id & account_id
            resp_w1 = await router.route_and_execute(
                db,
                ProviderRequest(prompt="Worker 1 backend schema check", agent_id="worker-1", dry_run=False),
                preferred_provider_id="arena-local-bridge",
            )
            resp_w3 = await router.route_and_execute(
                db,
                ProviderRequest(prompt="Worker 3 browser policy check", agent_id="worker-3", dry_run=False),
                preferred_provider_id="arena-local-bridge",
            )
            assert resp_w1.success is True
            assert resp_w3.success is True
            assert resp_w1.metadata["session_id"] == "sess-worker-1"
            assert resp_w3.metadata["session_id"] == "sess-worker-3"
            assert resp_w1.metadata["account_id"] == "acct-arena-primary"
            assert resp_w3.metadata["account_id"] == "acct-arena-primary"
            assert resp_w1.metadata["session_id"] != resp_w3.metadata["session_id"]

            # Inspect each independent session directly via ArenaLocalBridgeAdapter.get_session()
            adapter = ArenaLocalBridgeAdapter(
                "arena-local-bridge",
                srv.base_url,
                "env:ARENA_BRIDGE_TOKEN",
                "arena-bridge-default",
            )
            bridge_s1 = await adapter.get_session("sess-worker-1", account_id="acct-arena-primary")
            bridge_s3 = await adapter.get_session("sess-worker-3", account_id="acct-arena-primary")
            assert bridge_s1 is not None and bridge_s3 is not None
            assert bridge_s1["agent_id"] == "worker-1"
            assert bridge_s3["agent_id"] == "worker-3"
            assert bridge_s1["turn_count"] == 1
            assert bridge_s3["turn_count"] == 1
            assert any("Worker 1" in m["content"] for m in bridge_s1["history"])
            assert all("Worker 3" not in m["content"] for m in bridge_s1["history"])
            assert any("Worker 3" in m["content"] for m in bridge_s3["history"])
            assert all("Worker 1" not in m["content"] for m in bridge_s3["history"])

            # Verify DB ProviderSession telemetry was updated per employee
            db_s1 = await db.get(ProviderSession, "sess-worker-1")
            db_s3 = await db.get(ProviderSession, "sess-worker-3")
            assert db_s1.message_count == 1
            assert db_s3.message_count == 1
            assert db_s1.last_message_at is not None
            assert db_s3.last_message_at is not None


@pytest.mark.asyncio
async def test_bridge_sessions_health_and_count_metrics_and_scoping(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "bridge-metrics-token-2026")
    auth = {"Authorization": "Bearer bridge-metrics-token-2026"}
    bridge_app = create_app()
    transport = ASGITransport(app=bridge_app)

    async with AsyncClient(transport=transport, base_url="http://bridge.test") as client:
        # Populate 2 sessions under acct-alpha (3 total turns) and 1 session under acct-beta (1 turn)
        for sid, acct, agent, prompt in (
            ("sess-a1", "acct-alpha", "worker-1", "Alpha one first"),
            ("sess-a1", "acct-alpha", "worker-1", "Alpha one second"),
            ("sess-a2", "acct-alpha", "worker-2", "Alpha two first"),
            ("sess-b1", "acct-beta", "worker-3", "Beta one first"),
        ):
            r = await client.post(
                "/v1/chat/completions",
                headers={**auth, "X-AATM-Session-Id": sid, "X-AATM-Account-Id": acct, "X-AATM-Agent-Id": agent},
                json={"model": "arena-bridge-default", "messages": [{"role": "user", "content": prompt}]},
            )
            assert r.status_code == 200

        # Global GET /v1/sessions health & aggregate metrics
        all_resp = await client.get("/v1/sessions", headers=auth)
        assert all_resp.status_code == 200
        all_data = all_resp.json()
        assert all_data["status"] == "ok"
        assert all_data["count"] == 3
        assert all_data["active_sessions"] == 3
        assert all_data["account_scope"] is None
        m = all_data["metrics"]
        assert m["total_sessions"] == 3
        assert m["total_turns"] == 4
        assert m["total_messages"] == 8
        assert m["total_prompt_tokens"] >= 4
        assert m["total_completion_tokens"] >= 4
        assert m["unique_accounts"] == 2
        assert m["unique_agents"] == 3
        assert m["accounts_breakdown"] == {"acct-alpha": 2, "acct-beta": 1}
        assert m["uptime_seconds"] >= 0.0

        # Account-scoped GET /v1/sessions leaks zero cross-account metrics or sessions
        beta_resp = await client.get("/v1/sessions", headers={**auth, "X-AATM-Account-Id": "acct-beta"})
        assert beta_resp.status_code == 200
        beta_data = beta_resp.json()
        assert beta_data["status"] == "ok"
        assert beta_data["account_scope"] == "acct-beta"
        assert beta_data["count"] == 1
        assert beta_data["active_sessions"] == 1
        assert beta_data["metrics"]["total_sessions"] == 1
        assert beta_data["metrics"]["total_turns"] == 1
        assert beta_data["metrics"]["unique_accounts"] == 1
        assert beta_data["metrics"]["accounts_breakdown"] == {"acct-beta": 1}
        assert [s["session_id"] for s in beta_data["sessions"]] == ["sess-b1"]


@pytest.mark.asyncio
async def test_bridge_sse_streaming_and_account_isolation(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "bridge-sse-token-2026")
    auth = {"Authorization": "Bearer bridge-sse-token-2026"}
    bridge_app = create_app()
    transport = ASGITransport(app=bridge_app)

    async with AsyncClient(transport=transport, base_url="http://bridge.test") as client:
        # Health reports streaming capability
        h = await client.get("/health", headers=auth)
        assert h.status_code == 200
        assert h.json()["streaming_supported"] is True

        # Streamed completion under acct-stream-a
        resp = await client.post(
            "/v1/chat/completions",
            headers={
                **auth,
                "X-AATM-Session-Id": "sess-stream-1",
                "X-AATM-Account-Id": "acct-stream-a",
                "X-AATM-Agent-Id": "worker-3",
            },
            json={
                "model": "arena-bridge-default",
                "stream": True,
                "messages": [{"role": "user", "content": "Stream isolated bridge response"}],
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert resp.headers["x-aatm-session-id"] == "sess-stream-1"
        assert resp.headers["x-aatm-account-id"] == "acct-stream-a"

        data_lines = [
            line[len("data: ") :]
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        assert data_lines[-1] == "[DONE]"
        chunks = [json.loads(raw) for raw in data_lines[:-1]]
        assert len(chunks) >= 3
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        reassembled = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        assert "sess-stream-1" in reassembled
        assert "acct-stream-a" in reassembled
        assert "Stream isolated bridge response" in reassembled
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert chunks[-1]["usage"]["total_tokens"] >= 2
        assert chunks[-1]["session"]["turn_count"] == 1

        # Verify streamed turn was recorded in isolated session store
        sess_detail = await client.get(
            "/v1/sessions/sess-stream-1",
            headers={**auth, "X-AATM-Account-Id": "acct-stream-a"},
        )
        assert sess_detail.status_code == 200
        assert sess_detail.json()["turn_count"] == 1

        # Cross-account SSE streaming attempt is rejected with 403 BEFORE streaming starts
        hijack = await client.post(
            "/v1/chat/completions",
            headers={
                **auth,
                "X-AATM-Session-Id": "sess-stream-1",
                "X-AATM-Account-Id": "acct-stream-evil",
            },
            json={
                "model": "arena-bridge-default",
                "stream": True,
                "messages": [{"role": "user", "content": "Cross-account hijack"}],
            },
        )
        assert hijack.status_code == 403


@pytest.mark.asyncio
async def test_adapter_delete_session_e2e_on_live_bridge(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "e2e-delete-token-2026")
    with _LiveBridgeProcess() as srv:
        adapter = ArenaLocalBridgeAdapter(
            "arena-local-bridge",
            srv.base_url,
            "env:ARENA_BRIDGE_TOKEN",
            "arena-bridge-default",
        )
        gen = await adapter.generate(
            ProviderRequest(
                prompt="Seed session prior to employee handoff",
                agent_id="worker-3",
                dry_run=False,
                metadata={"session_id": "sess-handoff-01", "account_id": "acct-arena-primary"},
            )
        )
        assert gen.success is True
        assert await adapter.get_session("sess-handoff-01", account_id="acct-arena-primary") is not None

        # Cross-account delete denied cleanly (403)
        denied = await adapter.delete_session("sess-handoff-01", account_id="acct-other")
        assert denied["deleted"] is False
        assert denied["status"] == "forbidden"
        assert denied["bridge_reachable"] is True

        # Authorized delete succeeds and removes session context
        deleted = await adapter.delete_session("sess-handoff-01", account_id="acct-arena-primary")
        assert deleted["deleted"] is True
        assert deleted["status"] == "deleted"
        assert deleted["bridge_reachable"] is True
        assert await adapter.get_session("sess-handoff-01", account_id="acct-arena-primary") is None

        # Second delete is cleanly idempotent (not_found)
        repeat = await adapter.delete_session("sess-handoff-01", account_id="acct-arena-primary")
        assert repeat["deleted"] is False
        assert repeat["status"] == "not_found"
        assert repeat["bridge_reachable"] is True

    # Offline bridge returns structured bridge_offline status instead of raising
    offline_res = await adapter.delete_session("sess-handoff-01", account_id="acct-arena-primary")
    assert offline_res["deleted"] is False
    assert offline_res["status"] == "bridge_offline"
    assert offline_res["bridge_reachable"] is False


@pytest.mark.asyncio
async def test_bridge_state_persistence_and_idle_ttl_eviction(monkeypatch, tmp_path):
    token_val = "bridge-persist-token-2026"
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", token_val)
    auth = {"Authorization": f"Bearer {token_val}"}
    state_file = tmp_path / "bridge_state.json"

    # 1. Write isolated sessions to a persistent bridge store
    app_a = create_app(state_file=state_file, session_ttl_seconds=300.0)
    async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://bridge.test") as client:
        h = await client.get("/health", headers=auth)
        assert h.status_code == 200
        assert h.json()["persistence_enabled"] is True
        assert h.json()["session_ttl_seconds"] == 300.0

        r1 = await client.post(
            "/v1/chat/completions",
            headers={**auth, "X-AATM-Session-Id": "sess-persist-1", "X-AATM-Account-Id": "acct-alpha", "X-AATM-Agent-Id": "worker-1"},
            json={"model": "arena-bridge-default", "messages": [{"role": "user", "content": "Persisted turn one"}]},
        )
        assert r1.status_code == 200
        r2 = await client.post(
            "/v1/chat/completions",
            headers={**auth, "X-AATM-Session-Id": "sess-persist-2", "X-AATM-Account-Id": "acct-beta", "X-AATM-Agent-Id": "worker-2"},
            json={"model": "arena-bridge-default", "messages": [{"role": "user", "content": "Beta persisted turn"}]},
        )
        assert r2.status_code == 200

    assert state_file.exists()
    # State file must never store Bearer auth tokens
    assert token_val not in state_file.read_text(encoding="utf-8")

    # 2. Simulate daemon restart via fresh create_app(state_file=...) — sessions & isolation survive!
    app_b = create_app(state_file=state_file, session_ttl_seconds=300.0)
    async with AsyncClient(transport=ASGITransport(app=app_b), base_url="http://bridge.test") as client:
        s1 = await client.get("/v1/sessions/sess-persist-1", headers={**auth, "X-AATM-Account-Id": "acct-alpha"})
        assert s1.status_code == 200
        assert s1.json()["turn_count"] == 1
        assert any("Persisted turn one" in m["content"] for m in s1.json()["history"])

        # Cross-account isolation still holds after reload
        hijack = await client.get("/v1/sessions/sess-persist-1", headers={**auth, "X-AATM-Account-Id": "acct-beta"})
        assert hijack.status_code == 403

    # 3. Idle-session TTL eviction removes stale sessions without cross-account leakage
    app_ttl = create_app(state_file=state_file, session_ttl_seconds=60.0)
    # Age only sess-persist-1 past TTL while keeping sess-persist-2 fresh
    app_ttl.state.session_store._sessions["sess-persist-1"].last_active_epoch = time.time() - 120.0
    async with AsyncClient(transport=ASGITransport(app=app_ttl), base_url="http://bridge.test") as client:
        list_resp = await client.get("/v1/sessions", headers=auth)
        assert list_resp.status_code == 200
        body = list_resp.json()
        assert body["count"] == 1
        assert [s["session_id"] for s in body["sessions"]] == ["sess-persist-2"]
        assert body["metrics"]["evicted_sessions"] == 1

        gone = await client.get("/v1/sessions/sess-persist-1", headers={**auth, "X-AATM-Account-Id": "acct-alpha"})
        assert gone.status_code == 404


@pytest.mark.asyncio
async def test_adapter_generate_stream_and_router_route_and_stream_e2e(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "e2e-stream-token-2026")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)

        with _LiveBridgeProcess() as srv:
            bridge_prov = await db.get(AIProvider, "arena-local-bridge")
            bridge_prov.endpoint_url = srv.base_url
            await db.commit()

            # 1. Direct ArenaLocalBridgeAdapter.generate_stream() against live SSE server
            adapter = ArenaLocalBridgeAdapter(
                "arena-local-bridge",
                srv.base_url,
                "env:ARENA_BRIDGE_TOKEN",
                "arena-bridge-default",
            )
            direct_chunks = [
                c
                async for c in adapter.generate_stream(
                    ProviderRequest(
                        prompt="Stream direct adapter verification message",
                        agent_id="worker-1",
                        dry_run=False,
                        metadata={"session_id": "sess-worker-1", "account_id": "acct-arena-primary"},
                    )
                )
            ]
            content_chunks = [c for c in direct_chunks if not c.done]
            done_chunks = [c for c in direct_chunks if c.done]
            assert len(content_chunks) >= 2
            assert len(done_chunks) == 1
            assert done_chunks[0].success is True
            assert done_chunks[0].metadata["confirmed_usage"] is True
            assert done_chunks[0].metadata["session_id"] == "sess-worker-1"
            reassembled = "".join(c.delta for c in content_chunks)
            assert "sess-worker-1" in reassembled
            assert "Stream direct adapter verification message" in reassembled

            # Cross-account stream directly against adapter fails cleanly (HTTP 403)
            bad_chunks = [
                c
                async for c in adapter.generate_stream(
                    ProviderRequest(
                        prompt="Unauthorized cross-account stream",
                        agent_id="worker-2",
                        dry_run=False,
                        metadata={"session_id": "sess-worker-1", "account_id": "acct-evil"},
                    )
                )
            ]
            assert len(bad_chunks) == 1
            assert bad_chunks[0].done is True
            assert bad_chunks[0].success is False
            assert bad_chunks[0].metadata["http_status"] == 403

            # 2. End-to-end ProviderRouter.route_and_stream() with DB session tracking
            router = ProviderRouter()
            routed_chunks = [
                c
                async for c in router.route_and_stream(
                    db,
                    ProviderRequest(prompt="Router streamed prompt for worker 3", agent_id="worker-3", dry_run=False),
                    preferred_provider_id="arena-local-bridge",
                )
            ]
            routed_deltas = [c.delta for c in routed_chunks if not c.done]
            routed_done = [c for c in routed_chunks if c.done]
            assert len(routed_deltas) >= 2
            assert len(routed_done) == 1
            assert routed_done[0].success is True
            assert routed_done[0].metadata["session_id"] == "sess-worker-3"
            assert routed_done[0].metadata["account_id"] == "acct-arena-primary"
            assert "Router streamed prompt for worker 3" in "".join(routed_deltas)

            db_s3 = await db.get(ProviderSession, "sess-worker-3")
            assert db_s3.message_count == 1
            assert db_s3.last_message_at is not None

        # 3. Offline bridge diagnostic fallback via ProviderRouter.route_and_stream()
        # Disable secondary providers so offline fallback activates deterministically
        for pid in ("arena-agent", "anthropic-claude"):
            p = await db.get(AIProvider, pid)
            if p:
                p.is_enabled = False
        await db.commit()

        router = ProviderRouter()
        fb_chunks = [
            c
            async for c in router.route_and_stream(
                db,
                ProviderRequest(prompt="Offline stream test", agent_id="worker-3", dry_run=False),
                preferred_provider_id="arena-local-bridge",
            )
        ]
        assert any("safe-diagnostic" in c.delta for c in fb_chunks if not c.done)
        assert fb_chunks[-1].done is True
        assert fb_chunks[-1].success is True
        assert fb_chunks[-1].metadata["mode"] == "bridge_offline_diagnostic"


class _MockUpstreamSSEServer:
    """Loopback OpenAI-compatible SSE upstream server to verify incremental passthrough."""

    def __init__(self, *, chunks: list[str] | None = None, fail_status: int | None = None) -> None:
        self.port = _free_loopback_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.chunks = chunks or ["Upstream chunk one. ", "Upstream chunk two."]
        self.fail_status = fail_status
        self.received_requests: list[dict[str, Any]] = []
        self.received_auth_headers: list[str | None] = []
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def _build_app(self) -> FastAPI:
        up_app = FastAPI()

        @up_app.get("/health")
        async def _health() -> dict[str, str]:
            return {"status": "ok"}

        @up_app.post("/v1/chat/completions", response_model=None)
        async def _completions(
            body: dict[str, Any],
            authorization: str | None = Header(default=None),
        ) -> Any:
            self.received_requests.append(body)
            self.received_auth_headers.append(authorization)
            if self.fail_status is not None:
                return JSONResponse(status_code=self.fail_status, content={"error": "upstream boom"})

            async def _gen() -> AsyncIterator[str]:
                role_evt = {
                    "id": "up-cmpl-1",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": body.get("model", "arena-default"),
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(role_evt)}\n\n"
                for piece in self.chunks:
                    await asyncio.sleep(0.01)
                    evt = {
                        "id": "up-cmpl-1",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": body.get("model", "arena-default"),
                        "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(evt)}\n\n"
                end_evt = {
                    "id": "up-cmpl-1",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": body.get("model", "arena-default"),
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 14, "completion_tokens": 9, "total_tokens": 23},
                }
                yield f"data: {json.dumps(end_evt)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(_gen(), media_type="text/event-stream")

        return up_app

    def __enter__(self) -> "_MockUpstreamSSEServer":
        config = uvicorn.Config(self._build_app(), host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("Mock upstream SSE server did not start")

    def __exit__(self, *_: object) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5.0)


@pytest.mark.asyncio
async def test_bridge_upstream_sse_incremental_passthrough_and_session_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARENA_BRIDGE_TOKEN", raising=False)
    up_secret = "upstream-" + "bearer-" + "test-" + "991"

    with _MockUpstreamSSEServer(chunks=["Delta A | ", "Delta B | ", "Delta C"]) as upstream:
        monkeypatch.setenv("ARENA_BRIDGE_UPSTREAM_URL", upstream.base_url)
        monkeypatch.setenv("ARENA_BRIDGE_UPSTREAM_API_KEY", up_secret)

        with _LiveBridgeProcess() as bridge:
            adapter = ArenaLocalBridgeAdapter(
                provider_id="arena-local-bridge",
                endpoint_url=bridge.base_url,
                credential_ref=None,
                default_model="arena-default",
            )
            chunks = [
                c
                async for c in adapter.generate_stream(
                    ProviderRequest(
                        prompt="Stream incrementally from upstream",
                        agent_id="worker-3",
                        metadata={"session_id": "sess-up-sse-1", "account_id": "acct-up-1"},
                        dry_run=False,
                    )
                )
            ]

            deltas = [c.delta for c in chunks if not c.done]
            assert deltas == ["Delta A | ", "Delta B | ", "Delta C"]
            assert chunks[-1].done is True
            assert chunks[-1].success is True
            assert chunks[-1].finish_reason == "stop"
            assert chunks[-1].input_tokens == 14
            assert chunks[-1].output_tokens == 9
            assert chunks[-1].metadata["confirmed_usage"] is True
            assert chunks[-1].metadata["bridge_session"]["turn_count"] == 1

            # Verify upstream received stream=True and Authorization header
            assert len(upstream.received_requests) == 1
            assert upstream.received_requests[0]["stream"] is True
            assert upstream.received_auth_headers[0] == f"Bearer {up_secret}"

            # Verify session metadata recorded turn and cross-account 403 rejects BEFORE hitting upstream
            async with AsyncClient(base_url=bridge.base_url) as client:
                sess_res = await client.get("/v1/sessions/sess-up-sse-1", headers={"X-AATM-Account-Id": "acct-up-1"})
                assert sess_res.status_code == 200
                summary = sess_res.json()
                assert summary["turn_count"] == 1
                assert summary["message_count"] >= 2
                assert summary["total_prompt_tokens"] == 14
                assert summary["total_completion_tokens"] == 9

                forged = await client.post(
                    "/v1/chat/completions",
                    headers={"X-AATM-Session-Id": "sess-up-sse-1", "X-AATM-Account-Id": "acct-other"},
                    json={"model": "arena-default", "stream": True, "messages": [{"role": "user", "content": "hijack"}]},
                )
                assert forged.status_code == 403
                # Upstream must NOT have been called on the rejected cross-account request
                assert len(upstream.received_requests) == 1

            # Second turn confirms prior streamed assistant reply is forwarded in upstream conversation history
            chunks_t2 = [
                c
                async for c in adapter.generate_stream(
                    ProviderRequest(
                        prompt="Follow-up turn",
                        agent_id="worker-3",
                        metadata={"session_id": "sess-up-sse-1", "account_id": "acct-up-1"},
                        dry_run=False,
                    )
                )
            ]
            assert chunks_t2[-1].success is True
            assert len(upstream.received_requests) == 2
            hist = upstream.received_requests[1]["messages"]
            assert any(m["role"] == "assistant" and m["content"] == "Delta A | Delta B | Delta C" for m in hist)


@pytest.mark.asyncio
async def test_bridge_upstream_sse_safe_error_chunk_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARENA_BRIDGE_TOKEN", raising=False)
    up_secret = "secret-upstream-" + "never-leak-" + "774"
    monkeypatch.setenv("ARENA_BRIDGE_UPSTREAM_API_KEY", up_secret)

    # 1. Upstream HTTP 500 during SSE -> structured safe error chunk + [DONE], no key leak
    with _MockUpstreamSSEServer(fail_status=500) as broken_upstream:
        monkeypatch.setenv("ARENA_BRIDGE_UPSTREAM_URL", broken_upstream.base_url)
        with _LiveBridgeProcess() as bridge:
            async with AsyncClient(base_url=bridge.base_url) as client:
                raw_res = await client.post(
                    "/v1/chat/completions",
                    headers={"X-AATM-Session-Id": "sess-up-err-1", "X-AATM-Account-Id": "acct-up-err"},
                    json={"model": "arena-default", "stream": True, "messages": [{"role": "user", "content": "test"}]},
                )
                assert raw_res.status_code == 200
                assert "text/event-stream" in raw_res.headers.get("content-type", "")
                assert up_secret not in raw_res.text
                assert "data: [DONE]" in raw_res.text
                assert '"upstream_http_error"' in raw_res.text

            adapter = ArenaLocalBridgeAdapter(
                provider_id="arena-local-bridge",
                endpoint_url=bridge.base_url,
                credential_ref=None,
                default_model="arena-default",
            )
            err_chunks = [
                c
                async for c in adapter.generate_stream(
                    ProviderRequest(
                        prompt="Trigger upstream HTTP 500",
                        agent_id="worker-3",
                        metadata={"session_id": "sess-up-err-2", "account_id": "acct-up-err"},
                        dry_run=False,
                    )
                )
            ]
            assert err_chunks[-1].done is True
            assert err_chunks[-1].success is False
            assert err_chunks[-1].finish_reason == "error"
            assert err_chunks[-1].metadata.get("upstream_stream_error") is True
            assert up_secret not in (err_chunks[-1].error_message or "")

    # 2. Unreachable upstream port during SSE -> safe upstream_stream_error chunk + [DONE]
    dead_port = _free_loopback_port()
    monkeypatch.setenv("ARENA_BRIDGE_UPSTREAM_URL", f"http://127.0.0.1:{dead_port}")
    with _LiveBridgeProcess() as bridge:
        async with AsyncClient(base_url=bridge.base_url) as client:
            raw_res = await client.post(
                "/v1/chat/completions",
                headers={"X-AATM-Session-Id": "sess-up-dead", "X-AATM-Account-Id": "acct-up-err"},
                json={"model": "arena-default", "stream": True, "messages": [{"role": "user", "content": "test"}]},
            )
            assert raw_res.status_code == 200
            assert "data: [DONE]" in raw_res.text
            assert '"upstream_stream_error"' in raw_res.text
            assert up_secret not in raw_res.text
