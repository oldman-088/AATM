"""B1 — live code stream (`agent.code`) behaviour.

Covers the required guarantees: frames are emitted for real write/edit, test/lint and review
actions; planted secrets never appear; frames are capped and throttled/batched; sensitive files are
never streamed; and the per-agent buffer endpoint works (auth enforced).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.events import event_bus
from app.core.security import create_access_token
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import Agent, AgentMessage, Project, Task, User
from app.runtime.agent_protocol import ActionRejected, ReviewAction, WriteAction
from app.runtime.code_stream import (
    MAX_FRAME_LINES,
    CodeStream,
    build_review_frame,
    build_test_frame,
    build_write_frame,
    code_stream,
)
from app.runtime.executor import agent_runtime
from app.runtime.loop.actions import ActionsMixin

# Planted fake credentials are assembled at runtime so the repository secret scan stays clean.
FAKE_TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
FAKE_PASSWORD = "hunter2-" + "not-a-real-password"


@pytest.fixture(autouse=True)
def clean_stream():
    code_stream.clear()
    yield
    code_stream.clear()


@pytest.fixture()
async def env(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b1-code-stream.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "WORKSPACES_DIR", tmp_path / "workspaces")
    async with factory() as db:
        await seed_initial_data(db)

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    yield factory
    app.dependency_overrides.pop(get_db, None)
    await engine.dispose()


@pytest.fixture()
def captured(monkeypatch):
    events: list[dict] = []
    original = event_bus.emit

    async def spy(event_type: str, payload: dict):
        events.append({"type": event_type, "payload": payload})
        return await original(event_type, payload)

    monkeypatch.setattr(event_bus, "emit", spy)
    return events


class _ActionsHarness(ActionsMixin):
    """Runs the real action implementations without the provider/claim machinery."""

    def __init__(self, executor=None) -> None:
        self.executor = executor or agent_runtime


async def _task_and_message(db, agent_id: str = "worker-1"):
    project = (await db.execute(select(Project))).scalars().first()
    agent = await db.get(Agent, agent_id)
    assert project is not None and agent is not None
    task = Task(
        project_id=project.id,
        title="Stream a service module",
        description="Write a module and run its tests.",
        status="in_progress",
        assigned_agent_id=agent.id,
    )
    db.add(task)
    await db.flush()
    message = AgentMessage(
        task_id=task.id,
        project_id=project.id,
        sender_id="manager-0",
        recipient_id=agent.id,
        channel_type="task",
        instructions="Write the module.",
        priority="high",
        permissions=agent.permissions,
        execution_status="running",
        delivery_status="acknowledged",
    )
    db.add(message)
    await db.commit()
    await db.refresh(task)
    await db.refresh(message)
    return task, message, agent


async def _headers(factory) -> dict[str, str]:
    async with factory() as db:
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        admin.must_change_password = False
        await db.commit()
        token = create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)
    return {"Authorization": f"Bearer {token}"}


def _code_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "agent.code"]


def test_write_frame_carries_language_content_and_diff():
    frame = build_write_frame(
        agent_id="worker-1",
        task_id="task-1",
        path="app/service.py",
        content="def health():\n    return {'status': 'ok'}\n",
        previous_content="def health():\n    return None\n",
        action="edit",
    )
    assert frame is not None
    assert frame["action"] == "edit"
    assert frame["language"] == "python"
    assert frame["path"] == "app/service.py"
    added = [line["text"] for line in frame["diff"] if line["type"] == "add"]
    removed = [line["text"] for line in frame["diff"] if line["type"] == "del"]
    assert "    return {'status': 'ok'}" in added
    assert "    return None" in removed


def test_frames_are_capped_to_400_lines_and_32kb():
    content = "\n".join(f"line_{index} = {index}" for index in range(1, 1201))
    frame = build_write_frame(agent_id="worker-1", task_id=None, path="big.py", content=content)
    assert frame is not None
    assert frame["truncated"] is True
    assert len(frame["content"].splitlines()) == MAX_FRAME_LINES
    assert len(frame["content"].encode("utf-8")) <= 32 * 1024
    assert "line_1200" not in frame["content"]


def test_sensitive_files_are_never_streamed():
    for path in (".env", ".env.production", "config/.env.local", ".git/config", "certs/server.key", "id_rsa", "deploy/token.txt"):
        assert build_write_frame(agent_id="worker-1", task_id=None, path=path, content="x = 1\n") is None, path


def test_planted_secrets_are_redacted_or_dropped():
    with_secret = build_write_frame(
        agent_id="worker-1",
        task_id=None,
        path="app/config.py",
        content=f'API_KEY = "{FAKE_TOKEN}"\nDB_PASSWORD = "{FAKE_PASSWORD}"\nprint("ok")\n',
    )
    assert with_secret is not None
    assert FAKE_TOKEN not in str(with_secret)
    assert FAKE_PASSWORD not in str(with_secret)
    assert "[REDACTED]" in with_secret["content"]

    # Assembled by concatenation so the repo-wide secret scanner does not flag this fixture
    # (same rule the QA engine applies to any committed `-----BEGIN ... PRIVATE KEY-----` block).
    dash = "-----"
    private_key = f"{dash}BEGIN RSA PRIVATE KEY{dash}\nMIIEow\n{dash}END RSA PRIVATE KEY{dash}\n"
    assert build_write_frame(agent_id="worker-1", task_id=None, path="app/keys.py", content=private_key) is None

    test_frame = build_test_frame(
        agent_id="worker-1",
        task_id=None,
        command=["pytest", "-q"],
        stdout=f"collected 3 items\nexport GITHUB_TOKEN={FAKE_TOKEN}\n",
        exit_code=0,
    )
    assert test_frame is not None
    assert FAKE_TOKEN not in str(test_frame)

    review_frame = build_review_frame(
        agent_id="reviewer-1",
        task_id=None,
        body=f"Please rotate the token={FAKE_TOKEN} before merging.",
        event="REQUEST_CHANGES",
    )
    assert review_frame is not None
    assert FAKE_TOKEN not in str(review_frame)


def test_throttle_batches_and_caps_events():
    stream = CodeStream(interval_seconds=0.1, max_events_per_flush=10, clock=lambda: 0.0)
    for index in range(50):
        stream.record(
            {
                "agent_id": "worker-1",
                "task_id": "t1",
                "action": "write",
                "path": f"file_{index}.py",
                "language": "python",
                "content": "x = 1",
            }
        )

    first = stream.take_due_events(now=0.0)
    assert len(first) == 1  # one batched event, not 50
    assert first[0]["agent_id"] == "worker-1"
    assert first[0]["count"] == 20  # MAX_FRAMES_PER_EVENT
    assert first[0]["path"] == "file_19.py"

    assert stream.take_due_events(now=0.05) == []  # still cooling down

    second = stream.take_due_events(now=0.2)
    assert len(second) == 1 and second[0]["count"] == 20
    third = stream.take_due_events(now=0.4)
    assert len(third) == 1 and third[0]["count"] == 10  # remainder
    assert stream.pending_count() == 0
    assert stream.next_flush_delay(now=1.0) is None


def test_flush_budget_limits_how_many_agents_emit_per_tick():
    stream = CodeStream(interval_seconds=0.1, max_events_per_flush=3, clock=lambda: 0.0)
    for index in range(6):
        stream.record({"agent_id": f"worker-{index}", "task_id": None, "action": "write", "path": "a.py", "content": "x"})
    assert len(stream.take_due_events(now=0.0)) == 3
    # The remaining three agents never emitted yet, so they are due immediately; after they emit,
    # the next allowed emit is one interval later.
    assert stream.next_flush_delay(now=0.0) == 0.0
    assert len(stream.take_due_events(now=0.0)) == 3
    assert stream.next_flush_delay(now=0.0) is None


def test_buffer_is_bounded_per_agent():
    stream = CodeStream(buffer_frames=5)
    for index in range(20):
        stream.record({"agent_id": "worker-1", "task_id": None, "action": "write", "path": f"f{index}.py", "content": "x"})
    buffered = stream.buffer("worker-1", 50)
    assert [frame["path"] for frame in buffered] == ["f15.py", "f16.py", "f17.py", "f18.py", "f19.py"]


async def test_write_action_emits_agent_code_and_endpoint_serves_the_buffer(env, captured, tmp_path):
    factory = env
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with factory() as db:
        task, message, agent = await _task_and_message(db)
        result, completed = await _ActionsHarness()._execute_action(
            db,
            message,
            task,
            agent,
            WriteAction(action="write", path="app/service.py", content="def health():\n    return {'status': 'ok'}\n"),
            work_root=workspace,
        )
        await db.commit()
        agent_id = agent.id
    assert "Wrote" in result and completed is False
    assert (workspace / "app" / "service.py").is_file()

    events = _code_events(captured)
    assert events, "agent.code must be emitted for a real workspace write"
    payload = events[0]["payload"]
    assert payload["agent_id"] == agent_id
    frame = payload["frames"][0]
    assert frame["path"] == "app/service.py"
    assert frame["language"] == "python"
    assert "def health" in frame["content"]
    assert frame["action"] == "write"

    headers = await _headers(factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/agents/{agent_id}/code-stream?limit=5", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["agent_id"] == agent_id and body["count"] >= 1
        assert body["frames"][-1]["path"] == "app/service.py"

        bulk = await client.get("/api/v1/code-stream?limit=3", headers=headers)
        assert bulk.status_code == 200 and agent_id in bulk.json()["agents"]

        assert (await client.get(f"/api/v1/agents/{agent_id}/code-stream")).status_code == 401
        assert (
            await client.get("/api/v1/agents/does-not-exist/code-stream", headers=headers)
        ).status_code == 404


async def test_sensitive_write_is_rejected_and_never_streamed(env, captured, tmp_path):
    factory = env
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with factory() as db:
        task, message, agent = await _task_and_message(db)
        with pytest.raises(ActionRejected):
            await _ActionsHarness()._execute_action(
                db,
                message,
                task,
                agent,
                WriteAction(action="write", path=".env", content="SECRET_KEY=abc\n"),
                work_root=workspace,
            )
    assert _code_events(captured) == []
    assert not (workspace / ".env").exists()


async def test_executor_write_endpoint_streams_and_edit_frames(env, captured, tmp_path):
    factory = env
    async with factory() as db:
        await _task_and_message(db)
        await agent_runtime.write_workspace_file(db, "worker-1", "notes.md", "# first\n")
        await agent_runtime.write_workspace_file(db, "worker-1", "notes.md", "# first\nsecond line\n")
    await code_stream.drain()  # the second write is coalesced into the next throttled batch
    frames = [
        frame
        for event in _code_events(captured)
        for frame in event["payload"]["frames"]
        if frame.get("path") == "notes.md"
    ]
    assert [frame["action"] for frame in frames] == ["write", "edit"]
    assert frames[0]["language"] == "markdown"
    assert any(line["type"] == "add" for line in frames[1]["diff"])


async def test_run_action_streams_real_pytest_output(env, captured, tmp_path):
    factory = env
    from app.employees.catalog import ensure_employee_workspace

    workspace = ensure_employee_workspace("worker-1")
    async with factory() as db:
        task, message, agent = await _task_and_message(db)
        harness = _ActionsHarness()
        await harness._execute_action(
            db,
            message,
            task,
            agent,
            WriteAction(action="write", path="test_sample.py", content="def test_ok():\n    assert 1 + 1 == 2\n"),
            work_root=workspace,
        )
        from app.runtime.agent_protocol import parse_action_response

        result, completed = await harness._execute_action(
            db,
            message,
            task,
            agent,
            parse_action_response('{"action":"run","command":["pytest","-q","test_sample.py"]}'),
            work_root=workspace,
        )
        await db.commit()
    await code_stream.drain()
    assert "exit code" in result.lower() and completed is False
    code_events = _code_events(captured)
    test_frames = [frame for event in code_events for frame in event["payload"]["frames"] if frame["action"] == "test"]
    assert test_frames, "a real pytest run must stream a test frame"
    frame = test_frames[-1]
    assert frame["ok"] is True, frame
    assert frame["exit_code"] == 0
    assert frame["command"].startswith("pytest")
    assert any("passed" in line for line in frame["output_lines"])


async def test_review_action_streams_the_comment(env, captured, monkeypatch):
    from app.orchestration.review import review_service

    factory = env

    async def fake_submit_review(db, task, agent, *, event, body):
        return {"event": event, "follow_up_task_id": None, "merge_approval_id": "approval-1"}

    monkeypatch.setattr(review_service, "submit_review", fake_submit_review)
    async with factory() as db:
        task, message, agent = await _task_and_message(db)
        task.task_type = "review"
        await db.commit()
        _, completed = await _ActionsHarness()._execute_action(
            db,
            message,
            task,
            agent,
            ReviewAction(action="review", event="COMMENT", body="Nit: rename the helper for clarity."),
        )
        await db.commit()
    assert completed is True
    frames = [frame for event in _code_events(captured) for frame in event["payload"]["frames"]]
    assert frames and frames[-1]["action"] == "review"
    assert frames[-1]["review_event"] == "COMMENT"
    assert "rename the helper" in "\n".join(frames[-1]["output_lines"])


async def test_publish_flushes_pending_frames_on_timer(captured):
    stream = CodeStream(interval_seconds=0.02)
    for index in range(3):
        await stream.publish(
            {"agent_id": "worker-9", "task_id": "t", "action": "write", "path": f"x{index}.py", "content": "x = 1"}
        )
    assert stream.pending_count() >= 1  # coalesced, not emitted one-by-one
    await stream.drain()
    assert stream.pending_count() == 0
    events = _code_events(captured)
    emitted = sum(event["payload"]["count"] for event in events)
    assert emitted == 3
    assert len(events) < 3  # batching happened
