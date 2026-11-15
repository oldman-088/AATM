import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
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
