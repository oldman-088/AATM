from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_provider_connection_phase2 import FastAPIBridgeServer, ScriptedReply

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.models import (
    Agent,
    AgentMessage,
    AIProvider,
    ApprovalRequest,
    MemoryEntry,
    Project,
    ProviderUsageLog,
    SystemSetting,
    Task,
    WorkspaceArtifact,
)
from app.orchestration.scheduler import task_scheduler
from app.providers.base import ProviderResponse
from app.runtime.agent_loop import AgentLoop, parse_action_response


@pytest.fixture
async def db_session(monkeypatch, tmp_path) -> AsyncSession:
    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        yield db


def _completion(text: str) -> ScriptedReply:
    return ScriptedReply(
        body={
            "id": "phase3-loop",
            "model": "agent",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20},
        }
    )


class ScriptedRouter:
    def __init__(self, replies: list[str], after_reply=None):
        self.replies = deque(replies)
        self.after_reply = after_reply
        self.requests = []

    async def route_and_execute(self, db, request, preferred_provider_id=None):
        self.requests.append((request, preferred_provider_id))
        if self.after_reply:
            await self.after_reply(db, request, len(self.requests))
        content = self.replies.popleft()
        return ProviderResponse(
            provider_id=preferred_provider_id or "fake-provider",
            model=request.model or "fake-model",
            content=content,
            input_tokens=5,
            output_tokens=4,
            estimated_cost_usd=0.0,
            latency_ms=1.0,
            success=True,
        )


async def _make_running_message(
    db: AsyncSession,
    *,
    agent_id: str = "worker-1",
    expected: list[str] | None = None,
    collaborators: list[str] | None = None,
    dependencies: list[str] | None = None,
) -> tuple[Task, AgentMessage, Agent]:
    project = (await db.execute(select(Project).where(Project.slug == "aatm-core"))).scalar_one()
    agent = await db.get(Agent, agent_id)
    assert agent is not None
    task = Task(
        project_id=project.id,
        title="Build a small verified deliverable",
        description="Implement the requested code, test it, and report the actual files.",
        status="in_progress",
        assigned_agent_id=agent.id,
        collaborator_ids=collaborators or [],
        dependencies=dependencies or [],
        expected_deliverables=expected or [],
        lock_owner=agent.id,
    )
    db.add(task)
    await db.flush()
    message = AgentMessage(
        task_id=task.id,
        project_id=project.id,
        sender_id="manager-0",
        recipient_id=agent.id,
        channel_type="task",
        instructions=f"[AUTOMATIC] {task.title}: {task.description}",
        priority="high",
        required_tools=["workspace_fs", "python_runner"],
        workspace_info={},
        expected_deliverables=task.expected_deliverables,
        permissions=agent.permissions or [],
        execution_status="running",
        delivery_status="acknowledged",
    )
    agent.status = "working"
    agent.current_task_id = task.id
    db.add(message)
    await db.commit()
    await db.refresh(task)
    await db.refresh(message)
    return task, message, agent


async def _configure_bridge(db: AsyncSession, base_url: str, monkeypatch) -> None:
    monkeypatch.setenv("PHASE3_BRIDGE_TOKEN", "phase3-fake-bridge-token")
    result = await db.execute(select(AIProvider))
    for provider in result.scalars():
        provider.is_enabled = provider.id == "arena-local-bridge"
    bridge = await db.get(AIProvider, "arena-local-bridge")
    assert bridge is not None
    bridge.endpoint_url = base_url
    bridge.credential_ref = "env:PHASE3_BRIDGE_TOKEN"
    bridge.default_model = "agent"
    bridge.available_models = ["agent"]
    agent = await db.get(Agent, "worker-1")
    assert agent is not None
    agent.provider_id = bridge.id
    agent.model_name = "agent"
    await db.commit()


async def test_fake_bridge_executes_write_test_run_done_sequence(db_session, monkeypatch):
    replies = [
        _completion('{"action":"write","path":"fib.py","content":"def fib(n):\\n    if n < 2: return n\\n    a, b = 0, 1\\n    for _ in range(n): a, b = b, a + b\\n    return a\\n"}'),
        _completion('{"action":"write","path":"test_fib.py","content":"from fib import fib\\n\\ndef test_fib():\\n    assert fib(7) == 13\\n"}'),
        _completion('{"action":"run","command":["pytest","-q","test_fib.py"]}'),
        _completion('{"action":"done","summary":"Implemented Fibonacci and verified the test.","deliverables":["fib.py","test_fib.py"]}'),
    ]
    with FastAPIBridgeServer(expected_bearer="phase3-fake-bridge-token", chat_replies=replies) as bridge_server:
        await _configure_bridge(db_session, bridge_server.base_url, monkeypatch)
        task, message, _ = await _make_running_message(db_session, expected=["fib.py", "test_fib.py"])
        loop = AgentLoop(poll_seconds=0.01)

        assert await loop.process_one() is True

        await db_session.refresh(task)
        await db_session.refresh(message)
        assert task.status == "completed"
        assert task.actual_deliverables == ["fib.py", "test_fib.py"]
        assert message.execution_status == "completed"
        assert message.claimed_by is not None
        assert bridge_server.bearer_checks and all(bridge_server.bearer_checks)
        assert len(bridge_server.chat_requests) == 4
        assert bridge_server.chat_requests[0]["payload"]["messages"]
        artifacts = list(
            (await db_session.execute(select(WorkspaceArtifact).where(WorkspaceArtifact.task_id == task.id)))
            .scalars()
            .all()
        )
        assert {artifact.action for artifact in artifacts} >= {"create", "test", "agent_iteration"}
        memories = list(
            (await db_session.execute(select(MemoryEntry).where(MemoryEntry.task_id == task.id))).scalars().all()
        )
        assert len(memories) == 1 and "completed" in memories[0].content


def test_agent_loop_configuration_defaults_follow_bridge_concurrency(monkeypatch):
    for key in (
        "AGENT_LOOP_ENABLED",
        "AGENT_LOOP_POLL_SECONDS",
        "AGENT_MAX_ITERATIONS",
        "AGENT_MAX_PARALLEL",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None, ARENA_BRIDGE_MAX_CONCURRENCY=4)
    assert settings.AGENT_LOOP_ENABLED is True
    assert settings.AGENT_LOOP_POLL_SECONDS == 3.0
    assert settings.AGENT_MAX_ITERATIONS == 8
    assert settings.AGENT_MAX_PARALLEL == 4


def test_action_protocol_requires_one_valid_object_and_accepts_one_json_fence():
    parsed = parse_action_response('```json\n{"action":"done","summary":"All verified."}\n```')
    assert parsed.action == "done"
    with pytest.raises(ValueError):
        parse_action_response('commentary {"action":"done","summary":"No."}')
    with pytest.raises(ValueError):
        parse_action_response('{"action":"done","summary":"First."} {"action":"done","summary":"Second."}')
    with pytest.raises(ValueError):
        parse_action_response('{"action":"invent","path":"anything"}')


async def test_invalid_json_gets_correction_then_recovers(db_session):
    router = ScriptedRouter(["not json", '{"action":"done","summary":"Recovered using a valid action."}'])
    task, message, agent = await _make_running_message(db_session)
    loop = AgentLoop(provider_router=router)

    assert await loop.process_one()
    await db_session.refresh(task)
    await db_session.refresh(message)
    assert task.status == "completed"
    assert message.execution_status == "completed"
    assert len(router.requests) == 2
    assert router.requests[0][0].agent_id == agent.id
    assert router.requests[0][0].task_id == task.id
    second_prompt = json.loads(router.requests[1][0].prompt)
    assert second_prompt["employee"]["role_template_title"] == "Backend Developer"
    assert "Invalid action response" in second_prompt["previous_iterations"][0]["correction"]


async def test_iteration_limit_fails_through_scheduler_with_real_error(db_session):
    router = ScriptedRouter(["{}", "still not JSON"])
    task, message, _ = await _make_running_message(db_session)
    loop = AgentLoop(provider_router=router, max_iterations=2)

    assert await loop.process_one()
    await db_session.refresh(task)
    await db_session.refresh(message)
    assert task.status == "queued"
    assert task.retry_count == 1
    assert "iteration limit reached (2)" in (task.error_message or "").lower()
    assert message.execution_status == "failed"
    assert len(router.requests) == 2


async def test_emergency_stop_during_provider_turn_prevents_returned_action(db_session):
    async def stop_after_request(db: AsyncSession, request, call_number: int) -> None:
        if call_number != 1:
            return
        agent = await db.get(Agent, request.agent_id)
        task = await db.get(Task, request.task_id)
        assert agent is not None and task is not None
        agent.is_emergency_stopped = True
        agent.status = "paused"
        agent.current_task_id = None
        task.status = "blocked"
        task.lock_owner = None
        task.error_message = "Emergency stop (employee): test stop"
        await db.commit()

    router = ScriptedRouter(
        ['{"action":"write","path":"must_not_exist.py","content":"print(1)"}'],
        after_reply=stop_after_request,
    )
    task, message, agent = await _make_running_message(db_session)
    loop = AgentLoop(provider_router=router)

    assert await loop.process_one()
    await db_session.refresh(task)
    await db_session.refresh(message)
    assert task.status == "blocked"
    assert message.execution_status == "paused"
    assert message.claimed_by is None
    assert not (get_settings().WORKSPACES_DIR / agent.id / "must_not_exist.py").exists()


async def test_workspace_path_escape_is_rejected_without_external_write(db_session, tmp_path):
    router = ScriptedRouter(
        [
            '{"action":"write","path":"../../outside.py","content":"unsafe = True"}',
            '{"action":"write","path":"safe.py","content":"safe = True\\n"}',
            '{"action":"done","summary":"Wrote a safe file.","deliverables":["safe.py"]}',
        ]
    )
    task, message, agent = await _make_running_message(db_session, expected=["safe.py"])
    loop = AgentLoop(provider_router=router)

    assert await loop.process_one()
    await db_session.refresh(task)
    await db_session.refresh(message)
    root = get_settings().WORKSPACES_DIR / agent.id
    assert task.status == "completed"
    assert (root / "safe.py").is_file()
    assert not (tmp_path / "outside.py").exists()
    assert message.response_payload["iterations"][0]["action"] == "write"
    assert "rejected" in message.response_payload["iterations"][0]["result"].lower()


async def test_approval_pauses_and_resumes_after_approval_decision(db_session):
    router = ScriptedRouter(
        [
            '{"action":"approval","action_type":"deploy","title":"Deploy review","description":"Review deployment request."}',
            '{"action":"done","summary":"Completed safely after review."}',
        ]
    )
    task, message, agent = await _make_running_message(db_session)
    loop = AgentLoop(provider_router=router)

    assert await loop.process_one()
    await db_session.refresh(task)
    await db_session.refresh(message)
    approval = await db_session.get(ApprovalRequest, message.response_payload["approval_id"])
    assert approval is not None and approval.status == "pending"
    assert task.status == "blocked"
    assert message.execution_status == "paused"
    assert message.response_payload["approval_id"] == approval.id
    assert agent.status in {"idle", "working"}

    approval.status = "approved"
    approval.reviewed_by = "operator@example.test"
    approval.review_note = "Approved for this request."
    await db_session.commit()
    assert await loop.resume_approval_messages() == 1
    await db_session.refresh(task)
    await db_session.refresh(message)
    assert task.status == "in_progress"
    assert message.execution_status == "running"

    assert await loop.process_one()
    await db_session.refresh(task)
    await db_session.refresh(message)
    assert task.status == "completed"
    assert message.execution_status == "completed"
    assert len(router.requests) == 2


async def test_collaborator_message_uses_existing_handoff_path(db_session):
    router = ScriptedRouter(
        [
            '{"action":"message","recipient_id":"worker-2","instructions":"Review the current source and report issues."}',
            '{"action":"done","summary":"Delivered the collaboration handoff."}',
        ]
    )
    task, _, agent = await _make_running_message(db_session, collaborators=["worker-2"])
    workspace = get_settings().WORKSPACES_DIR / agent.id
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "draft.py").write_text("answer = 42\n", encoding="utf-8")
    (workspace / ".env").write_text("TOKEN=must-not-copy\n", encoding="utf-8")
    loop = AgentLoop(provider_router=router)

    assert await loop.process_one()
    handoff = get_settings().WORKSPACES_DIR / "worker-2" / f"handoff_from_{agent.id}"
    assert (handoff / "draft.py").read_text(encoding="utf-8") == "answer = 42\n"
    assert not (handoff / ".env").exists()
    delegated = (
        await db_session.execute(
            select(AgentMessage).where(
                AgentMessage.task_id == task.id,
                AgentMessage.recipient_id == "worker-2",
            )
        )
    ).scalar_one()
    assert delegated.execution_status == "delivered"
    assert delegated.workspace_info["handoff_path"] == f"handoff_from_{agent.id}"


async def test_atomic_message_claim_and_task_lock(db_session):
    task, message, _ = await _make_running_message(db_session)
    first = AgentLoop(worker_id="loop-a")
    second = AgentLoop(worker_id="loop-b")

    claimed = await first.claim_one()
    assert claimed == message.id
    assert await second.claim_one() is None
    await db_session.refresh(message)
    assert message.claimed_by == "loop-a"
    assert message.claimed_at is not None

    await task_scheduler.release_lock(db_session, task)
    assert await task_scheduler.acquire_lock(db_session, task, "worker-1") is True
    assert await task_scheduler.acquire_lock(db_session, task, "worker-2") is False
    await db_session.refresh(task)
    assert task.lock_owner == "worker-1"


async def test_disallowed_shell_or_unknown_executable_never_runs(db_session, tmp_path):
    marker = tmp_path / "phase3-should-not-exist"
    unsafe_run = json.dumps({"action": "run", "command": ["bash", "-c", f"touch {marker}"]})
    router = ScriptedRouter(
        [
            unsafe_run,
            '{"action":"done","summary":"Completed without executing a shell.","deliverables":[]}',
        ]
    )
    task, message, _ = await _make_running_message(db_session)
    loop = AgentLoop(provider_router=router)

    assert await loop.process_one()
    await db_session.refresh(task)
    await db_session.refresh(message)
    assert task.status == "completed"
    assert "rejected" in message.response_payload["iterations"][0]["result"].lower()
    assert not marker.exists()


async def test_dependency_result_is_handed_off_and_included_in_prompt(db_session):
    project_id = (await db_session.execute(select(Project).where(Project.slug == "aatm-core"))).scalar_one().id
    dependency = Task(
        project_id=project_id,
        title="Create shared module",
        description="The dependency produced a reusable module.",
        status="completed",
        assigned_agent_id="worker-2",
        result_summary="A shared helper is ready.",
        actual_deliverables=["result.py"],
    )
    db_session.add(dependency)
    await db_session.commit()
    source_workspace = get_settings().WORKSPACES_DIR / "worker-2"
    source_workspace.mkdir(parents=True, exist_ok=True)
    (source_workspace / "result.py").write_text("answer = 42\n", encoding="utf-8")
    router = ScriptedRouter(['{"action":"done","summary":"Used the completed dependency."}'])
    task, _, agent = await _make_running_message(db_session, dependencies=[dependency.id])
    loop = AgentLoop(provider_router=router)

    assert await loop.process_one()
    prompt = json.loads(router.requests[0][0].prompt)
    assert "A shared helper is ready." in prompt["dependency_results"][0]["summary"]
    assert prompt["dependency_results"][0]["handoff_path"] == "handoff_from_worker-2"
    assert (get_settings().WORKSPACES_DIR / agent.id / "handoff_from_worker-2" / "result.py").is_file()
    await db_session.refresh(task)
    assert task.status == "completed"


async def test_per_task_cost_budget_stops_before_another_provider_turn(db_session):
    budgets = await db_session.get(SystemSetting, "budgets")
    assert budgets is not None
    budgets.value = {**budgets.value, "per_task_limit_usd": 0.25}
    task, message, agent = await _make_running_message(db_session)
    db_session.add(
        ProviderUsageLog(
            provider_id="arena-local-bridge",
            agent_id=agent.id,
            task_id=task.id,
            model_name="agent",
            estimated_cost_usd=0.30,
        )
    )
    await db_session.commit()
    router = ScriptedRouter(['{"action":"done","summary":"Must not be reached."}'])
    loop = AgentLoop(provider_router=router)

    assert await loop.process_one()
    await db_session.refresh(task)
    await db_session.refresh(message)
    assert router.requests == []
    assert "cost budget reached" in (task.error_message or "").lower()
    assert "$0.3000 of $0.2500" in (task.error_message or "")
    assert message.execution_status == "failed"


async def test_executor_does_not_inherit_application_credentials(db_session, monkeypatch):
    monkeypatch.setenv("PHASE3_APP_SECRET", "must-never-reach-pytest")
    workspace = get_settings().WORKSPACES_DIR / "worker-1"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "test_env.py").write_text(
        """import os

def test_application_secret_is_absent():
    assert 'PHASE3_APP_SECRET' not in os.environ
""",
        encoding="utf-8",
    )
    router = ScriptedRouter(
        [
            '{"action":"run","command":["pytest","-q","test_env.py"]}',
            '{"action":"done","summary":"The sandbox test passed."}',
        ]
    )
    task, message, _ = await _make_running_message(db_session)
    loop = AgentLoop(provider_router=router)

    assert await loop.process_one()
    await db_session.refresh(task)
    await db_session.refresh(message)
    assert task.status == "completed"
    assert "exit code: 0" in message.response_payload["iterations"][0]["result"].lower()
    assert "must-never-reach-pytest" not in message.response_payload["iterations"][0]["result"]


async def test_fastapi_lifespan_cancels_background_scheduler_and_agent_loop(db_session, monkeypatch):
    from app import main
    from app.runtime import agent_loop as agent_loop_module
    from app.runtime import background_scheduler as background_scheduler_module

    agent_started = asyncio.Event()
    agent_stopped = asyncio.Event()
    orchestration_started = asyncio.Event()
    orchestration_stopped = asyncio.Event()
    self_healing_started = asyncio.Event()
    self_healing_stopped = asyncio.Event()

    class RecordingLoop:
        async def run_forever(self):
            agent_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                agent_stopped.set()

    class RecordingBackgroundScheduler:
        async def run_orchestration_forever(self):
            orchestration_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                orchestration_stopped.set()

        async def run_self_healing_forever(self):
            self_healing_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self_healing_stopped.set()

    monkeypatch.setattr(agent_loop_module, "AgentLoop", RecordingLoop)
    monkeypatch.setattr(background_scheduler_module, "BackgroundScheduler", RecordingBackgroundScheduler)
    monkeypatch.setattr(get_settings(), "AGENT_LOOP_ENABLED", True)
    async with main.lifespan(main.app):
        await asyncio.wait_for(agent_started.wait(), timeout=1.0)
        await asyncio.wait_for(orchestration_started.wait(), timeout=1.0)
        await asyncio.wait_for(self_healing_started.wait(), timeout=1.0)
        assert isinstance(main.app.state.agent_loop, RecordingLoop)
        assert isinstance(main.app.state.background_scheduler, RecordingBackgroundScheduler)
    assert agent_stopped.is_set()
    assert orchestration_stopped.is_set()
    assert self_healing_stopped.is_set()
