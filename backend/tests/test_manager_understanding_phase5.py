from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_provider_connection_phase2 import FastAPIBridgeServer, ScriptedReply

from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.models import Agent, AIProvider, ApprovalRequest, ChatMessage, Project, Task
from app.orchestration.scheduler import task_scheduler


@pytest.fixture
async def db_session(monkeypatch, tmp_path) -> AsyncSession:
    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "BROWSER_PROFILES_DIR", tmp_path / "browser_profiles")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        yield db


def _reply(content: str) -> ScriptedReply:
    return ScriptedReply(
        body={
            "id": "phase5-manager",
            "model": "agent",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 60},
        }
    )


def _plan_reply(reply: str, tasks: list[dict[str, Any]]) -> ScriptedReply:
    return _reply(json.dumps({"reply": reply, "tasks": tasks}, ensure_ascii=False))


async def _configure_bridge(db: AsyncSession, base_url: str, monkeypatch) -> None:
    token_name = "PHASE5_MANAGER_BRIDGE_TOKEN"
    monkeypatch.setenv(token_name, "phase5-fake-bridge-token")
    providers = list((await db.execute(select(AIProvider))).scalars().all())
    for provider in providers:
        provider.is_enabled = provider.id == "arena-local-bridge"
    bridge = await db.get(AIProvider, "arena-local-bridge")
    manager = await db.get(Agent, "manager-0")
    assert bridge is not None
    assert manager is not None
    bridge.endpoint_url = base_url
    bridge.credential_ref = f"env:{token_name}"
    bridge.default_model = "agent"
    bridge.available_models = ["agent"]
    manager.provider_id = bridge.id
    manager.model_name = "agent"
    await db.commit()


async def _core_project(db: AsyncSession) -> Project:
    return (await db.execute(select(Project).where(Project.slug == "aatm-core"))).scalar_one()


def _task_plan(
    title: str,
    division_code: str,
    assigned_agent_id: str,
    *,
    depends_on: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "description": f"Implement and verify {title.lower()}.",
        "division_code": division_code,
        "assigned_agent_id": assigned_agent_id,
        "expected_deliverables": [f"{title.lower().replace(' ', '-')}-verified"],
        "depends_on": depends_on or [],
    }


@pytest.mark.asyncio
async def test_arabic_directive_uses_bridge_context_and_creates_assigned_dependent_tasks(
    db_session, monkeypatch
):
    project = await _core_project(db_session)
    plan = [
        _task_plan("Projects API endpoint", "D02", "worker-1"),
        _task_plan("Projects dashboard view", "D03", "worker-2", depends_on=[0]),
    ]
    response_text = "سأنشئ نقطة النهاية أولاً، ثم ستتبعها واجهة المشاريع."

    with FastAPIBridgeServer(
        expected_bearer="phase5-fake-bridge-token",
        chat_replies=[_reply("Not valid JSON."), _plan_reply(response_text, plan)],
    ) as bridge:
        await _configure_bridge(db_session, bridge.base_url, monkeypatch)
        directive = "أضف نقطة نهاية للمشاريع، ثم أنشئ واجهة تعرضها بعد اكتمال نقطة النهاية."
        message = await task_scheduler.handle_manager_directive(db_session, project.id, directive)

        assert len(bridge.chat_requests) == 2
        payload = bridge.chat_requests[0]["payload"]
        assert payload["user"] == "manager-0"
        assert bridge.chat_requests[0]["headers"]["x-aatm-agent-id"] == "manager-0"
        prompt = payload["messages"][-1]["content"]
        assert directive in prompt
        assert '"skills"' in prompt and "fastapi" in prompt
        assert '"open_tasks"' in prompt
        correction_prompt = bridge.chat_requests[1]["payload"]["messages"][-1]["content"]
        assert "failed validation" in correction_prompt

    assert message.content == response_text
    saved_message = await db_session.get(ChatMessage, message.id)
    assert saved_message is not None and saved_message.content == response_text
    created = list(
        (
            await db_session.execute(
                select(Task).where(Task.title.in_(["Projects API endpoint", "Projects dashboard view"]))
            )
        )
        .scalars()
        .all()
    )
    by_title = {task.title: task for task in created}
    assert set(by_title) == {"Projects API endpoint", "Projects dashboard view"}
    assert by_title["Projects API endpoint"].assigned_agent_id == "worker-1"
    assert by_title["Projects dashboard view"].assigned_agent_id == "worker-2"
    assert by_title["Projects API endpoint"].execution_mode == "manual"
    assert by_title["Projects dashboard view"].execution_mode == "manual"
    assert by_title["Projects API endpoint"].dependencies == []
    assert by_title["Projects dashboard view"].dependencies == [by_title["Projects API endpoint"].id]
    assert by_title["Projects dashboard view"].expected_deliverables == ["projects-dashboard-view-verified"]
    assert message.metadata_json["task_ids"]
    assert "orchestration" in message.metadata_json


@pytest.mark.asyncio
async def test_cyclic_manager_dependencies_are_rejected_without_persisting_tasks(db_session, monkeypatch):
    project = await _core_project(db_session)
    cycle = [
        _task_plan("Cycle API task", "D02", "worker-1", depends_on=[1]),
        _task_plan("Cycle QA task", "D11", "worker-4", depends_on=[0]),
    ]
    with FastAPIBridgeServer(
        expected_bearer="phase5-fake-bridge-token",
        chat_replies=[_plan_reply("I proposed two tasks.", cycle)],
    ) as bridge:
        await _configure_bridge(db_session, bridge.base_url, monkeypatch)
        message = await task_scheduler.handle_manager_directive(
            db_session,
            project.id,
            "Please plan an API task followed by a QA task.",
        )

    assert "circular dependency" in message.content.lower()
    assert "no tasks were created" in message.content.lower()
    titles = {"Cycle API task", "Cycle QA task"}
    found = list((await db_session.execute(select(Task).where(Task.title.in_(titles)))).scalars())
    assert found == []


@pytest.mark.asyncio
async def test_high_risk_directive_still_creates_approval_and_does_not_queue_risky_task(
    db_session, monkeypatch
):
    project = await _core_project(db_session)
    risky_plan = [_task_plan("Deploy to production", "D12", "worker-4")]
    with FastAPIBridgeServer(
        expected_bearer="phase5-fake-bridge-token",
        chat_replies=[_plan_reply("سيبقى النشر معلقاً حتى الموافقة.", risky_plan)],
    ) as bridge:
        await _configure_bridge(db_session, bridge.base_url, monkeypatch)
        directive = "Deploy the service to production now."
        message = await task_scheduler.handle_manager_directive(db_session, project.id, directive)

    approvals = list(
        (
            await db_session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.action_type == "deploy",
                    ApprovalRequest.description.contains(directive),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    assert any("Approval Request" in action for action in message.metadata_json["actions"])
    assert "Task creation deferred" in " ".join(message.metadata_json["actions"])
    risky_tasks = list(
        (await db_session.execute(select(Task).where(Task.title == "Deploy to production"))).scalars()
    )
    assert risky_tasks == []


@pytest.mark.asyncio
async def test_no_enabled_provider_returns_honest_message_without_creating_tasks(db_session):
    project = await _core_project(db_session)
    for provider in (await db_session.execute(select(AIProvider))).scalars():
        provider.is_enabled = False
    await db_session.commit()

    message = await task_scheduler.handle_manager_directive(
        db_session,
        project.id,
        "أريد خطة قصيرة لتحديث المستندات.",
    )

    assert "no model provider configured" in message.content.lower()
    assert "no tasks were created" in message.content.lower()
    assert "Manager Directive Acknowledged" not in message.content
    assert list((await db_session.execute(select(ChatMessage).where(ChatMessage.id == message.id))).scalars())
    assert list((await db_session.execute(select(Task).where(Task.title == "Documentation refresh"))).scalars()) == []


@pytest.mark.asyncio
async def test_invalid_json_gets_one_correction_then_an_honest_error(db_session, monkeypatch):
    project = await _core_project(db_session)
    with FastAPIBridgeServer(
        expected_bearer="phase5-fake-bridge-token",
        chat_replies=[_reply("This is not JSON."), _reply("Still not JSON.")],
    ) as bridge:
        await _configure_bridge(db_session, bridge.base_url, monkeypatch)
        message = await task_scheduler.handle_manager_directive(
            db_session,
            project.id,
            "Plan a documented maintenance task.",
        )
        assert len(bridge.chat_requests) == 2
        second_prompt = bridge.chat_requests[1]["payload"]["messages"][-1]["content"]
        assert "failed validation" in second_prompt
        assert "This is not JSON." in second_prompt

    assert "could not validate a task plan" in message.content.lower()
    assert "no tasks were created" in message.content.lower()
    assert list((await db_session.execute(select(Task).where(Task.title == "Maintenance task"))).scalars()) == []
