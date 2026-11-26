"""E66: the prompt's own example JSON never becomes a plan. E68: a worker window is resumed when its check clears.

E66: on 2026-11-26 the manager page handed back AATM's own plan prompt; its reply format (every field "...")
parsed as a valid plan and a task titled "..." was created for worker-1.
E68: worker-1 hit a human check at 05:10 and the watchdog saw the page clean again, but the worker stayed
needs_human and its task stayed blocked until somebody pressed Resume, so six dependent tasks waited for hours.
"""

# ruff: noqa: F811 - pytest fixtures are re-exported
from __future__ import annotations

import pytest
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures

from app.browser import arena_sessions
from app.db.base import utcnow
from app.models import Agent, AuditLog, BrowserProfile, Project, Task
from app.orchestration.arena_prompts import (
    PLAN_FORMAT,
    extract_manager_json,
    is_prompt_echo,
    manager_marker,
    validate_manager_plan,
)

# ---------------------------------------------------------------- E66


def test_the_reply_format_example_is_not_a_plan():
    example = '{"summary_ar": "...", "tasks": [{"title": "...", "assignee": "worker-1", "description": "...", ' \
              '"acceptance_criteria": ["..."], "depends_on": []}]}'
    plan, error = validate_manager_plan(extract_manager_json(example), {"worker-1"})
    assert plan is None and "summary_ar" in error


@pytest.mark.parametrize("placeholder", ["...", "…", " <...> ", "-", "___"])
def test_placeholder_fields_are_refused(placeholder):
    data = {"summary_ar": "خطة", "tasks": [{"title": placeholder, "assignee": "worker-1", "description": "real work",
                                            "acceptance_criteria": ["it runs"], "depends_on": []}]}
    plan, error = validate_manager_plan(data, {"worker-1"})
    assert plan is None and "title" in error


def test_real_short_text_still_counts():
    data = {"summary_ar": "خطة قصيرة.", "tasks": [{"title": "API v2...", "assignee": "worker-1", "description": "x",
                                                   "acceptance_criteria": ["ok"], "depends_on": []}]}
    plan, _ = validate_manager_plan(data, {"worker-1"})
    assert plan is not None


def test_a_reply_that_is_the_prompt_is_recognised():
    marker = manager_marker("plan")
    assert is_prompt_echo(f"{marker} You are manager-0 ...\n```json\n{PLAN_FORMAT}\n```", "plan")
    # the page's Markdown export escapes the brackets: "\[AATM-MANAGER:PLAN\]"
    assert is_prompt_echo("\\[AATM-MANAGER:PLAN\\] You are manager-0", "plan")
    assert not is_prompt_echo('```json\n{"summary_ar": "خطة", "tasks": []}\n```', "plan")


# ---------------------------------------------------------------- E68


async def _stuck_worker(env, agent_id: str = "worker-1") -> tuple[str, str]:
    async with env() as db:
        project = Project(name="Heal", slug="heal", repo_path="/tmp/none", default_branch="main")
        db.add(project)
        await db.flush()
        agent = await db.get(Agent, agent_id)
        task = Task(project_id=project.id, title="backend core", description="x", status="blocked",
                    assigned_agent_id=agent.id, execution_mode="manual",
                    checkpoints=[{"ts": utcnow().isoformat(), "event": "needs_human"}])
        db.add(task)
        agent.status = "needs_human"
        profile = await db.get(BrowserProfile, agent.browser_profile_id)
        profile.connector_status = "needs_human"
        profile.needs_human_reason = "human check element on page"
        await db.commit()
        return profile.id, task.id


def _bridge(monkeypatch, profiles: list[dict]):
    async def call(method, path, *args, **kwargs):
        assert (method, path) == ("GET", "/v1/connector/profiles")
        return {"ok": True, "profiles": profiles}

    monkeypatch.setattr(arena_sessions, "bridge_call", call)
    monkeypatch.setattr(arena_sessions, "arena_gate", lambda: {"enabled": True})


async def test_a_window_the_bridge_calls_ready_is_resumed(env, monkeypatch):
    profile_id, task_id = await _stuck_worker(env)
    _bridge(monkeypatch, [{"profile_id": profile_id, "connected": True, "status": "ready"}])
    async with env() as db:
        assert await arena_sessions.resume_cleared_profiles(db) == [profile_id]
    async with env() as db:
        assert (await db.get(Task, task_id)).status == "queued"
        assert (await db.get(Agent, "worker-1")).status == "idle"
        profile = await db.get(BrowserProfile, profile_id)
        assert profile.connector_status == "ready" and profile.needs_human_reason is None
        audits = [a async for a in await db.stream_scalars(
            AuditLog.__table__.select().where(AuditLog.action == "arena.human_resolved"))]
        assert audits, "the automatic resume is audited like the owner's Resume"


@pytest.mark.parametrize("live", [
    {"connected": True, "status": "needs_human"},   # the check is still on the page
    {"connected": False, "status": "ready"},        # the window is gone; check_windows reopens it first
])
async def test_a_window_still_stuck_is_left_alone(env, monkeypatch, live):
    profile_id, task_id = await _stuck_worker(env)
    _bridge(monkeypatch, [{"profile_id": profile_id, **live}])
    async with env() as db:
        assert await arena_sessions.resume_cleared_profiles(db) == []
    async with env() as db:
        assert (await db.get(Task, task_id)).status == "blocked"
        assert (await db.get(Agent, "worker-1")).status == "needs_human"
