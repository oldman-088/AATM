from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import Agent, AgentMessage, Project, ProviderUsageLog, Task
from app.providers.adapters import ArenaLocalBridgeAdapter, compute_cost_details
from app.providers.base import ProviderRequest
from app.providers.router import get_model_configuration_status, provider_router
from app.runtime.agent_loop import AgentLoop
from app.runtime.executor import AgentRuntimeExecutor


@pytest.fixture
async def db_session():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        yield db


@pytest.fixture
async def client(db_session):
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def auth_headers(client: AsyncClient):
    settings = get_settings()
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    data = resp.json()
    token = data["access_token"]
    if data["user"].get("must_change_password"):
        rot = await client.post(
            "/api/v1/auth/change-password",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "current_password": settings.DEFAULT_ADMIN_PASSWORD,
                "new_password": "TestSuiteRotatedPassword!2026",
            },
        )
        assert rot.status_code == 200
        token = rot.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class _MockOpenAIHandler(BaseHTTPRequestHandler):
    requests_log: list[dict] = []
    mode: str = "simple"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        payload = json.loads(raw or "{}")
        self.__class__.requests_log.append(payload)

        messages = payload.get("messages") or []
        combined_text = json.dumps(messages, ensure_ascii=False)

        if self.__class__.mode == "arabic_e2e":
            if "assigned_agent_id" in combined_text:
                reply_obj = {
                    "reply": "تم تحليل التوجيه العربي وإنشاء خطة التنفيذ.",
                    "tasks": [
                        {
                            "title": "Create hello.py and test_hello.py",
                            "description": "Create hello.py printing Hello AATM and verify with pytest.",
                            "division_code": "D02",
                            "assigned_agent_id": "worker-1",
                            "expected_deliverables": ["hello.py", "test_hello.py"],
                            "depends_on": [],
                        }
                    ],
                }
                content = json.dumps(reply_obj, ensure_ascii=False)
            elif "to hello.py." not in combined_text:
                content = json.dumps(
                    {
                        "action": "write_file",
                        "path": "hello.py",
                        "content": "def greet() -> str:\n    return 'Hello AATM'\n\nif __name__ == '__main__':\n    print(greet())\n",
                    }
                )
            elif "to test_hello.py." not in combined_text:
                content = json.dumps(
                    {
                        "action": "write_file",
                        "path": "test_hello.py",
                        "content": "from hello import greet\n\ndef test_greet():\n    assert greet() == 'Hello AATM'\n",
                    }
                )
            elif "Command exit code: 0" not in combined_text:
                content = json.dumps(
                    {
                        "action": "run",
                        "command": ["pytest", "-q", "test_hello.py"],
                    }
                )
            else:
                content = json.dumps(
                    {
                        "action": "done",
                        "summary": "Created hello.py and test_hello.py and verified passing tests.",
                        "deliverables": ["hello.py", "test_hello.py"],
                    }
                )
        else:
            content = "ok"

        body = json.dumps(
            {
                "id": "chatcmpl-phase2",
                "object": "chat.completion",
                "model": payload.get("model", "qwen2.5-coder:7b"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_mock_openai(mode: str = "simple") -> tuple[HTTPServer, str]:
    _MockOpenAIHandler.requests_log = []
    _MockOpenAIHandler.mode = mode
    server = HTTPServer(("127.0.0.1", 0), _MockOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"


def test_pricing_catalog_marks_arena_and_unknown_models_unknown_cost():
    arena_cost, arena_known = compute_cost_details("arena-reasoning-pro", 2000, 1000)
    assert arena_cost == 0.0
    assert arena_known is False

    unknown_cost, unknown_known = compute_cost_details("custom-unlisted-model-xyz", 1000, 500)
    assert unknown_cost == 0.0
    assert unknown_known is False

    claude_cost, claude_known = compute_cost_details("claude-sonnet-4-5", 1000, 500)
    assert claude_known is True
    assert claude_cost > 0.0


@pytest.mark.asyncio
async def test_bridge_diagnostic_only_mode_and_adapter_rejection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARENA_BRIDGE_UPSTREAM_URL", "")
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "dummy-bridge-token")

    class _DiagHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802
            body = json.dumps(
                {
                    "id": "diag-1",
                    "mode": "diagnostic_only",
                    "diagnostic_only": True,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "echo"}, "finish_reason": "stop"}],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), _DiagHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        adapter = ArenaLocalBridgeAdapter(
            "arena-local-bridge",
            f"http://127.0.0.1:{srv.server_port}",
            "env:ARENA_BRIDGE_TOKEN",
            "arena-bridge-default",
        )
        res = await adapter.generate(ProviderRequest(prompt="test", dry_run=False, allow_diagnostic_fallback=False))
        assert res.success is False
        assert "diagnostic_only" in (res.error_message or "")
    finally:
        srv.shutdown()


@pytest.mark.asyncio
async def test_no_model_configured_status_and_blocks(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ARENA_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("ARENA_BRIDGE_UPSTREAM_URL", raising=False)

    status = await get_model_configuration_status(db_session)
    assert status["status"] == "NO_MODEL_CONFIGURED"
    assert status["configured"] is False

    health_res = await client.get("/api/health")
    assert health_res.status_code == 200
    assert health_res.json()["model_status"]["status"] == "NO_MODEL_CONFIGURED"

    overview_res = await client.get("/api/v1/dashboard/overview", headers=auth_headers)
    assert overview_res.status_code == 200
    assert overview_res.json()["model_status"]["status"] == "NO_MODEL_CONFIGURED"

    before_tasks = list((await db_session.execute(select(Task))).scalars().all())
    chat_res = await client.post(
        "/api/v1/chat",
        headers=auth_headers,
        json={"content": "build a Python calculator with unit tests", "project_id": "aatm-core"},
    )
    assert chat_res.status_code == 201
    data = chat_res.json()
    assert "NO_MODEL_CONFIGURED" in data["manager_reply"]["content"]
    assert data["manager_reply"]["metadata"]["model_status"]["status"] == "NO_MODEL_CONFIGURED"
    after_tasks = list((await db_session.execute(select(Task))).scalars().all())
    assert len(after_tasks) == len(before_tasks)


@pytest.mark.asyncio
async def test_wizard_endpoints_and_per_employee_model_assignment(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    presets_res = await client.get("/api/v1/providers/wizard/presets", headers=auth_headers)
    assert presets_res.status_code == 200
    options = presets_res.json()["options"]
    # R7 added the "Arena Agent (browser)" option; the three original presets are unchanged.
    assert len(options) == 4
    assert [o["provider_type"] for o in options] == ["anthropic_claude", "openai_compatible", "arena_local_bridge", "arena_agent"]
    arena_agent = options[-1]
    assert arena_agent["requires_api_key"] is False and arena_agent["credential_ref"] == "env:ARENA_BRIDGE_TOKEN"

    bad_cred = await client.post(
        "/api/v1/providers/wizard/test-connection",
        headers=auth_headers,
        json={
            "provider_type": "openai_compatible",
            "endpoint_url": "https://api.example.test/v1",
            "credential_ref": "sk-inline-secret-value",
            "model": "qwen2.5-coder:7b",
        },
    )
    assert bad_cred.status_code == 400

    monkeypatch.setenv("TEST_WIZARD_OLLAMA_API_KEY", "dummy-wizard-token")
    srv, base_url = _start_mock_openai("simple")
    try:
        test_res = await client.post(
            "/api/v1/providers/wizard/test-connection",
            headers=auth_headers,
            json={
                "provider_type": "openai_compatible",
                "endpoint_url": base_url,
                "credential_ref": "env:TEST_WIZARD_OLLAMA_API_KEY",
                "model": "qwen2.5-coder:7b",
            },
        )
        assert test_res.status_code == 200
        assert test_res.json()["success"] is True
        assert _MockOpenAIHandler.requests_log[-1]["max_tokens"] == 1

        cfg_res = await client.post(
            "/api/v1/providers/wizard/configure",
            headers=auth_headers,
            json={
                "provider_type": "openai_compatible",
                "endpoint_url": base_url,
                "credential_ref": "env:TEST_WIZARD_OLLAMA_API_KEY",
                "default_model": "qwen2.5-coder:7b",
                "available_models": ["qwen2.5-coder:7b", "llama3.1:8b"],
                "assign_to_manager": True,
                "assign_to_workers": True,
            },
        )
        assert cfg_res.status_code == 200
        cfg_data = cfg_res.json()
        assert cfg_data["model_status"]["status"] == "CONFIGURED"
        assert "manager-0" in cfg_data["updated_agents"]
    finally:
        srv.shutdown()

    patch_res = await client.patch(
        "/api/v1/agents/worker-1",
        headers=auth_headers,
        json={"provider_id": "openai-compatible", "model_name": "llama3.1:8b"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["provider_id"] == "openai-compatible"
    assert patch_res.json()["model_name"] == "llama3.1:8b"

    bad_patch = await client.patch(
        "/api/v1/agents/worker-1",
        headers=auth_headers,
        json={"provider_id": "missing-provider"},
    )
    assert bad_patch.status_code == 404

    db_session.add(
        ProviderUsageLog(
            provider_id="arena-agent",
            agent_id="worker-1",
            model_name="arena-reasoning-pro",
            input_tokens=100,
            output_tokens=50,
            estimated_cost_usd=0.0,
            pricing_known=False,
            latency_ms=10.0,
            success=True,
        )
    )
    await db_session.commit()
    usage_res = await client.get("/api/v1/providers/usage", headers=auth_headers)
    assert usage_res.status_code == 200
    assert usage_res.json()["summary"]["has_unknown_pricing"] is True


@pytest.mark.asyncio
async def test_phase2_arabic_directive_end_to_end_creates_hello_and_runs_pytest(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setenv("TEST_ARABIC_MODEL_API_KEY", "dummy-arabic-e2e-token")
    srv, base_url = _start_mock_openai("arabic_e2e")
    try:
        cfg_res = await client.post(
            "/api/v1/providers/wizard/configure",
            headers=auth_headers,
            json={
                "provider_type": "openai_compatible",
                "endpoint_url": base_url,
                "credential_ref": "env:TEST_ARABIC_MODEL_API_KEY",
                "default_model": "qwen2.5-coder:7b",
                "assign_to_manager": True,
                "assign_to_workers": True,
            },
        )
        assert cfg_res.status_code == 200
        assert cfg_res.json()["model_status"]["configured"] is True

        await db_session.execute(delete(AgentMessage))
        await db_session.execute(delete(Task))
        await db_session.commit()

        project = (await db_session.execute(select(Project).where(Project.slug == "aatm-core"))).scalar_one()
        chat_res = await client.post(
            "/api/v1/chat",
            headers=auth_headers,
            json={
                "content": "أنشئ ملف hello.py يطبع Hello AATM واكتب له اختبارًا وشغّله",
                "project_id": project.id,
            },
        )
        assert chat_res.status_code == 201
        task = (await db_session.execute(select(Task).where(Task.title == "Create hello.py and test_hello.py"))).scalar_one()
        msg = (
            await db_session.execute(select(AgentMessage).where(AgentMessage.task_id == task.id))
        ).scalar_one_or_none()
        if msg is None:
            cycle_res = await client.post("/api/v1/orchestration/cycle", headers=auth_headers)
            assert cycle_res.status_code == 200
            msg = (
                await db_session.execute(select(AgentMessage).where(AgentMessage.task_id == task.id))
            ).scalar_one()

        agent = await db_session.get(Agent, msg.recipient_id)
        assert agent is not None
        ws_dir = tmp_path / agent.id
        ws_dir.mkdir(parents=True, exist_ok=True)

        class _LocalTestExecutor(AgentRuntimeExecutor):
            async def run_workspace_command(self, db, agent_id, command, *, project_id=None, task_id=None):
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    *command,
                    cwd=str(tmp_path / agent_id),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=30)
                return {
                    "exit_code": proc.returncode or 0,
                    "stdout": out_b.decode("utf-8", errors="replace"),
                    "stderr": err_b.decode("utf-8", errors="replace"),
                }

        loop = AgentLoop(
            session_factory=AsyncSessionLocal,
            executor=_LocalTestExecutor(),
            provider_router=provider_router,
            max_iterations=6,
        )
        processed = await loop.process_one()
        assert processed is True

        await db_session.refresh(task)
        assert task.status == "completed"
        assert (ws_dir / "hello.py").exists()
        assert (ws_dir / "test_hello.py").exists()
        assert "Hello AATM" in (ws_dir / "hello.py").read_text(encoding="utf-8")
    finally:
        srv.shutdown()
