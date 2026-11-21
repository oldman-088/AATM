"""R7 — the manager (manager-0) on Arena Agent mode, like the workers.

R7.2: the ``Arena Agent (browser)`` wizard option, ``AATM_MANAGER_PROVIDER`` and the provider card's
health, which is healthy only when the manager's own Arena profile is connected and its Selector
test passed (otherwise it says why, in Arabic).
"""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_d_arena_connector
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from test_b_review_merge_completion import REPO_SLUG, _FnRouter, _git, _init_repo
from test_d_arena_connector import (  # noqa: F401 - fixtures
    _admin_headers,
    arena_settings,
    bridge,
    env,
    needs_chromium,
)
from test_r3_arena_agent_workflow import ArenaOnGitHub, FakeGitHub, _drive, _passthrough, _report

from app.browser.arena_connector import MANAGER_PROFILE_ID
from app.core.config import Settings
from app.main import app
from app.models import (
    Agent,
    AIProvider,
    ApprovalRequest,
    ArenaSession,
    BrowserProfile,
    ChatMessage,
    ManagerJob,
    Notification,
    Project,
    ProviderUsageLog,
    SystemSetting,
    Task,
)
from app.orchestration import manager_arena
from app.orchestration.arena_prompts import (
    MANAGER_JSON_RETRY,
    MAX_PLAN_TASKS,
    build_manager_plan_prompt,
    build_manager_review_prompt,
    extract_manager_json,
    truncate_diff,
    validate_manager_plan,
    validate_manager_review,
)
from app.orchestration.manager_arena import (
    MANAGER_PROVIDER_SETTING,
    NOT_CONNECTED_AR,
    ManagerArenaRuntime,
    classify_owner_message,
    manager_arena_runtime,
    manager_provider_mode,
    manager_readiness,
)
from app.providers.adapters.arena_agent import ArenaAgentAdapter


def _mock_mode(arena_settings, monkeypatch, bridge) -> None:
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", f"{bridge.base}/mock-arena/")
    monkeypatch.setattr(arena_settings, "ARENA_BRIDGE_URL", bridge.base)


# --------------------------------------------------------------------------- R7.1 profile
async def test_manager_is_seeded_as_its_own_agent_mode_arena_profile(env):
    async with env() as db:
        manager = await db.get(Agent, "manager-0")
        profile = await db.get(BrowserProfile, MANAGER_PROFILE_ID)
        assert manager.browser_profile_id == MANAGER_PROFILE_ID
        assert profile.name == "manager" and profile.agent_id == "manager-0" and profile.arena_mode == "agent"
        assert profile.profile_dir.endswith("/manager")
        # Workers keep their own profiles; the manager's profile is not shared.
        workers = list((await db.execute(select(Agent).where(Agent.role == "worker"))).scalars())
        assert MANAGER_PROFILE_ID not in {w.browser_profile_id for w in workers}


# --------------------------------------------------------------------------- R7.2 setting
def test_manager_provider_setting_accepts_only_the_documented_values(monkeypatch):
    for value in ("arena_agent", "anthropic_claude", "openai_compatible", "demo", ""):
        assert Settings(AATM_MANAGER_PROVIDER=value).AATM_MANAGER_PROVIDER == value
    with pytest.raises(ValidationError):
        Settings(AATM_MANAGER_PROVIDER="gpt5")


async def test_env_setting_overrides_the_wizard_choice(env, arena_settings, monkeypatch):
    monkeypatch.setattr(arena_settings, "AATM_MANAGER_PROVIDER", "")
    async with env() as db:
        assert await manager_provider_mode(db) == ""
        db.add(SystemSetting(key=MANAGER_PROVIDER_SETTING, value={"provider_type": "arena_agent"}))
        await db.commit()
        assert await manager_provider_mode(db) == "arena_agent"
        monkeypatch.setattr(arena_settings, "AATM_MANAGER_PROVIDER", "demo")
        assert await manager_provider_mode(db) == "demo"


async def test_demo_manager_provider_answers_labelled_and_creates_nothing(env, arena_settings, monkeypatch):
    monkeypatch.setattr(arena_settings, "AATM_MANAGER_PROVIDER", "demo")
    headers = await _admin_headers(env)
    async with env() as db:
        before = len(list((await db.execute(select(Task))).scalars()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/chat", json={"content": "ابنِ صفحة تسجيل دخول"}, headers=headers)
    assert r.status_code == 201, r.text
    reply = r.json()["manager_reply"]
    assert "DEMO" in reply["content"] and reply["metadata"]["demo"] is True
    async with env() as db:
        assert len(list((await db.execute(select(Task))).scalars())) == before


# --------------------------------------------------------------------------- R7.2 health / wizard
async def test_provider_card_explains_in_arabic_why_arena_agent_is_degraded(env, bridge, arena_settings, monkeypatch):
    adapter = ArenaAgentAdapter()
    off = await adapter.health_check()
    assert off.status == "degraded" and off.detail.startswith("not_enabled") and "ARENA_CONNECTOR_ENABLED" in off.detail

    _mock_mode(arena_settings, monkeypatch, bridge)
    unregistered = await adapter.health_check()
    assert unregistered.status == "degraded" and "توليد السكربت" in unregistered.detail

    headers = await _admin_headers(env)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/api/v1/arena/profiles/{MANAGER_PROFILE_ID}/userscript", headers=headers)
        assert r.status_code == 200 and r.json()["bridge_registration"]["ok"], r.text
        assert "AATM Arena Connector - profile-manager" in r.json()["script"]
    assert bridge.hub.profiles[MANAGER_PROFILE_ID].mode == "agent"
    offline = await adapter.health_check()
    assert offline.status == "degraded" and NOT_CONNECTED_AR in offline.detail
    readiness = await manager_readiness()
    assert readiness == {**readiness, "ready": False, "code": "offline", "reason_ar": NOT_CONNECTED_AR}


async def test_wizard_offers_arena_agent_tests_the_manager_profile_and_assigns_it(env, bridge, arena_settings, monkeypatch):
    _mock_mode(arena_settings, monkeypatch, bridge)
    monkeypatch.setattr(arena_settings, "AATM_MANAGER_PROVIDER", "")
    headers = await _admin_headers(env)
    body = {"provider_type": "arena_agent", "endpoint_url": bridge.base, "credential_ref": "env:ARENA_BRIDGE_TOKEN",
            "model": "arena-agent-orchestrator"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        presets = (await client.get("/api/v1/providers/wizard/presets", headers=headers)).json()
        option = next(o for o in presets["options"] if o["provider_type"] == "arena_agent")
        assert option["title"] == "Arena Agent (browser)" and option["requires_api_key"] is False
        assert len(presets["options"]) == 4

        tested = await client.post("/api/v1/providers/wizard/test-connection", json=body, headers=headers)
        assert tested.status_code == 200, tested.text
        assert tested.json()["success"] is False and "توليد السكربت" in tested.json()["detail"]

        configure = {**{k: v for k, v in body.items() if k != "model"}, "default_model": "arena-agent-orchestrator",
                     "assign_to_manager": True, "assign_to_workers": False}
        r = await client.post("/api/v1/providers/wizard/configure", json=configure, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["updated_agents"] == ["manager-0"] and r.json()["manager_provider"] == "arena_agent"
        assert r.json()["health"]["status"] == "degraded"
    async with env() as db:
        manager = await db.get(Agent, "manager-0")
        provider = await db.get(AIProvider, "arena-agent")
        assert manager.provider_id == "arena-agent" and provider.provider_type == "arena_agent"
        assert provider.priority != 1, "Arena Agent must not become every employee's first provider"
        assert provider.credential_ref == "env:ARENA_BRIDGE_TOKEN"
        assert await manager_provider_mode(db) == "arena_agent"
        # Workers keep their providers when only the manager is assigned.
        assert {w.provider_id for w in (await db.execute(select(Agent).where(Agent.role == "worker"))).scalars()} != {"arena-agent"}
        assert not list((await db.execute(select(ChatMessage).where(ChatMessage.sender_id == "manager-0",
                                                                    ChatMessage.content.contains("DEMO")))).scalars())


async def test_arena_agent_credential_is_bound_to_the_local_bridge(env, arena_settings, monkeypatch):
    headers = await _admin_headers(env)
    body = {"provider_type": "arena_agent", "endpoint_url": "https://evil.example.com", "credential_ref": "env:ARENA_BRIDGE_TOKEN",
            "default_model": "arena-agent-orchestrator", "assign_to_manager": True}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/providers/wizard/configure", json=body, headers=headers)
    assert r.status_code in {400, 403, 422}, r.text


# =========================================================================== R7.3 prompts / parsing

TEAM = {"worker-1", "worker-2"}


def _plan(**task_overrides) -> dict:
    task = {"title": "Add answer()", "assignee": "worker-1", "description": "answer() returns 42",
            "acceptance_criteria": ["answer() returns 42"], "depends_on": []}
    return {"summary_ar": "خطة", "tasks": [{**task, **task_overrides}]}


def test_owner_messages_are_classified_into_chat_plan_and_status():
    assert classify_owner_message("وين وصلتوا؟")[0] == "status"
    assert classify_owner_message("What is the status of the project?")[0] == "status"
    assert classify_owner_message("ما رأيك في استخدام Redis؟")[0] == "chat"
    assert classify_owner_message("ابنِ صفحة تسجيل دخول مع اختبارات")[0] == "plan"
    assert classify_owner_message("/chat ابنِ شيئًا") == ("chat", "ابنِ شيئًا")
    assert classify_owner_message("/goal أضف بحثًا") == ("plan", "أضف بحثًا")
    assert classify_owner_message("hello", "status")[0] == "status"


def test_manager_json_is_taken_from_the_first_fenced_block_or_the_first_balanced_object():
    assert extract_manager_json('Plan:\n```json\n{"a": 1}\n```\n```json\n{"a": 2}\n```') == {"a": 1}
    assert extract_manager_json('text {"a": {"b": "}"}} more {"c": 3}') == {"a": {"b": "}"}}
    assert extract_manager_json("no json here") is None
    assert extract_manager_json("```json\n[1, 2]\n```") is None


def test_plan_validation_rejects_unknown_assignees_empty_fields_too_many_tasks_and_bad_dependencies():
    plan, err = validate_manager_plan(_plan(), TEAM)
    assert err == "" and plan["tasks"][0]["assignee"] == "worker-1"
    assert "unknown assignee" in validate_manager_plan(_plan(assignee="ghost-9"), TEAM)[1]
    assert "title" in validate_manager_plan(_plan(title="  "), TEAM)[1]
    assert "acceptance_criteria" in validate_manager_plan(_plan(acceptance_criteria=[]), TEAM)[1]
    assert "summary_ar" in validate_manager_plan({**_plan(), "summary_ar": ""}, TEAM)[1]
    assert "earlier tasks" in validate_manager_plan(_plan(depends_on=[0]), TEAM)[1]
    many = {"summary_ar": "x", "tasks": _plan()["tasks"] * (MAX_PLAN_TASKS + 1)}
    assert "maximum is 10" in validate_manager_plan(many, TEAM)[1]
    assert validate_manager_plan(None, TEAM)[0] is None


def test_plan_size_cap_is_a_setting_and_zero_means_no_limit(monkeypatch):
    from app.core.config import get_settings

    big = {"summary_ar": "x", "tasks": _plan()["tasks"] * (MAX_PLAN_TASKS + 15)}
    assert "maximum is 10" in validate_manager_plan(big, TEAM)[1], "the default cap is unchanged"
    monkeypatch.setattr(get_settings(), "ARENA_MANAGER_MAX_PLAN_TASKS", 0)
    plan, err = validate_manager_plan(big, TEAM)
    assert err == "" and len(plan["tasks"]) == MAX_PLAN_TASKS + 15
    monkeypatch.setattr(get_settings(), "ARENA_MANAGER_MAX_PLAN_TASKS", 20)
    assert "maximum is 20" in validate_manager_plan({"summary_ar": "x", "tasks": _plan()["tasks"] * 21}, TEAM)[1]


def test_plan_prompt_states_the_cap_or_that_there_is_none(monkeypatch):
    from app.core.config import get_settings

    team = [{"id": "worker-1", "name": "Backend", "specialty": "FastAPI", "skills": ["python"]}]
    assert "At most 10 tasks" in build_manager_plan_prompt("goal", None, team, [])
    monkeypatch.setattr(get_settings(), "ARENA_MANAGER_MAX_PLAN_TASKS", 0)
    unlimited = build_manager_plan_prompt("goal", None, team, [])
    assert "At most" not in unlimited and "no limit on the number of tasks" in unlimited


def test_review_validation_requires_a_known_verdict_and_comments_for_request_changes():
    ok, _ = validate_manager_review({"verdict": "approve", "summary_ar": "جيد", "comments": []})
    assert ok["verdict"] == "approve"
    assert "at least one comment" in validate_manager_review({"verdict": "request_changes", "summary_ar": "x", "comments": []})[1]
    assert "verdict" in validate_manager_review({"verdict": "merge", "summary_ar": "x", "comments": []})[1]


def test_manager_prompts_carry_role_team_github_rules_json_format_and_truncate_large_diffs():
    project = Project(name="Demo", slug="demo", repo_url="https://github.com/acme/demo", default_branch="main")
    team = [{"id": "worker-1", "name": "Backend", "specialty": "FastAPI", "skills": ["python"]}]
    plan = build_manager_plan_prompt("ابنِ answer()", project, team, [])
    for needle in ("[AATM-MANAGER:PLAN]", "manager-0", "worker-1 — Backend: FastAPI", "Nobody merges", "[AATM-<task_id>]",
                   '"acceptance_criteria"', "ONLY one JSON block", "acme/demo"):
        assert needle in plan, needle
    task = Task(id="t-1", title="Add answer()", description="answer() returns 42", assigned_agent_id="worker-1")
    big = "+x\n" * 20_000
    shown, truncated = truncate_diff(big, url="https://github.com/acme/demo/pull/1")
    assert truncated and "DIFF TRUNCATED" in shown and "pull/1" in shown and len(shown) < len(big)
    review = build_manager_review_prompt(task, acceptance=["answer() returns 42"], report="## AATM REPORT", diff=big,
                                         pr_url="https://github.com/acme/demo/pull/1", round_number=1, project=project, team=team)
    for needle in ("[AATM-MANAGER:REVIEW]", "answer() returns 42", "AATM REPORT", "DIFF TRUNCATED", '"verdict"',
                   "approve does NOT merge"):
        assert needle in review, needle
    assert MANAGER_JSON_RETRY == "Reply again with only the JSON block, no other text."


# =========================================================================== R7.3 runtime (scripted page)

READY = {"ready": True, "code": "ready", "connected": True, "selector_ok": True, "selector_ran": True,
         "reason_ar": "ready", "profile_id": MANAGER_PROFILE_ID}


class ScriptedManagerPage(ArenaAgentAdapter):
    """A manager Arena page without a browser: replies come from ``answers`` keyed by the prompt marker."""

    def __init__(self, answers: dict[str, list[str]] | None = None) -> None:
        super().__init__()
        self.answers = {k: list(v) for k, v in (answers or {}).items()}
        self.page = "ready"
        self.since = 0
        self.busy_ticks = 0
        self.kw_rounds = 0
        self.stuck = False
        self.commands: list[tuple[str, str | None]] = []
        self.reply = ""
        self.continued_replies: list[str] = []
        self.last_kind = "CHAT"

    def _move(self, page: str) -> None:
        self.page, self.since = page, self.since + 1

    async def state(self, profile_id: str) -> dict[str, Any]:
        if self.page == "generating" and not self.stuck:
            if self.busy_ticks > 0:
                self.busy_ticks -= 1
            else:
                self._move("keep_working_visible" if self.kw_rounds > 0 else "idle")
        return {"ok": True, "connected": True, "mode": "agent", "status": "ready", "agent_state": self.page,
                "agent_state_since": str(self.since)}

    async def command(self, profile_id: str, command: str, text: str | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        self.commands.append((command, text))
        if command == "new_session":
            self._move("ready")
            return {"ok": True}
        if command == "submit":
            marker = re.search(r"\[AATM-MANAGER:([A-Z]+)\]", text or "")
            kind = marker.group(1) if marker else ("RETRY" if (text or "").startswith(MANAGER_JSON_RETRY) else "CHAT")
            self.last_kind = kind
            queue = self.answers.get(kind) or self.answers.get("*") or ["تم."]
            self.reply = queue.pop(0) if len(queue) > 1 else queue[0]
            self.busy_ticks = 1
            self._move("generating")
            return {"sent": True}
        if command == "keep_working":
            self.kw_rounds -= 1
            if self.continued_replies:
                self.reply = self.continued_replies.pop(0)
            self.busy_ticks = 1
            self._move("generating")
            return {"clicked": True}
        if command == "read_last_reply":
            return {"text": self.reply, "truncated": False}
        return {"ok": True}

    def submitted(self, kind: str | None = None) -> list[str]:
        return [t or "" for c, t in self.commands if c == "submit" and (kind is None or f"[AATM-MANAGER:{kind}]" in (t or ""))]


@pytest.fixture()
async def scripted(env, arena_settings, monkeypatch):
    async def _ready(profile_id: str = MANAGER_PROFILE_ID) -> dict[str, Any]:
        return dict(READY)

    monkeypatch.setattr(manager_arena, "manager_readiness", _ready)
    monkeypatch.setattr(arena_settings, "AATM_MANAGER_PROVIDER", "arena_agent")
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 0)

    def make(answers: dict[str, list[str]] | None = None) -> tuple[ScriptedManagerPage, ManagerArenaRuntime]:
        page = ScriptedManagerPage(answers)
        runtime = ManagerArenaRuntime(page, session_factory=env)
        runtime.poll_seconds = 0
        runtime.start_grace_seconds = 0
        return page, runtime

    async with env() as db:
        project = Project(name="R7", slug="r7", repo_url="https://github.com/acme/demo", default_branch="main")
        db.add(project)
        await db.commit()
        project_id = project.id
    return make, project_id


async def _jobs(env) -> list[ManagerJob]:
    async with env() as db:
        return list((await db.execute(select(ManagerJob).order_by(ManagerJob.created_at))).scalars())


async def test_invalid_json_gets_one_retry_then_needs_human_with_the_raw_reply(env, scripted):
    make, project_id = scripted
    page, runtime = make({"PLAN": ["Sure, here is my plan in words."], "RETRY": ["Still prose, sorry."]})
    async with env() as db:
        before = len(list((await db.execute(select(Task))).scalars()))
        reply = await runtime.handle_owner_message(db, project_id, "ابنِ دالة answer()")
    assert "Arena" in reply.content
    await runtime.wait_idle()
    job = next(j for j in await _jobs(env) if j.kind == "plan")
    assert job.status == "needs_human" and job.json_retries == 1
    assert job.raw_reply == "Still prose, sorry." and "invalid JSON" in job.error
    assert page.submitted("PLAN") and page.submitted().count(MANAGER_JSON_RETRY) == 1
    async with env() as db:
        assert len(list((await db.execute(select(Task))).scalars())) == before, "no plan may be invented"
        message = await db.get(ChatMessage, job.reply_message_id)
        assert message.metadata_json["needs_human"] is True and message.metadata_json["raw_reply"] == "Still prose, sorry."
        assert any(n.category == "manager" for n in (await db.execute(select(Notification))).scalars())


async def test_unknown_assignee_is_rejected_then_corrected_by_the_retry(env, scripted):
    make, project_id = scripted
    bad = '```json\n{"summary_ar": "خطة", "tasks": [{"title": "x", "assignee": "ghost", "description": "d", "acceptance_criteria": ["c"], "depends_on": []}]}\n```'
    good = '```json\n{"summary_ar": "خطة جيدة", "tasks": [{"title": "Add answer()", "assignee": "worker-1", "description": "answer() returns 42", "acceptance_criteria": ["answer() returns 42"], "depends_on": []}]}\n```'
    page, runtime = make({"PLAN": [bad], "RETRY": [good]})
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, "ابنِ دالة answer()")
    await runtime.wait_idle()
    job = next(j for j in await _jobs(env) if j.kind == "plan")
    assert job.status == "done" and job.json_retries == 1
    async with env() as db:
        task = await db.get(Task, job.result["task_ids"][0])
        assert task.assigned_agent_id == "worker-1" and task.execution_mode == "manual" and task.project_id == project_id
        assert task.checkpoints[0]["acceptance_criteria"] == ["answer() returns 42"]


async def test_long_lived_session_rotates_on_new_project_and_after_max_exchanges(env, scripted, arena_settings, monkeypatch):
    make, project_id = scripted
    monkeypatch.setattr(arena_settings, "ARENA_MANAGER_MAX_EXCHANGES", 4)
    page, runtime = make({"*": ["تم."]})
    async with env() as db:
        other = Project(name="Other", slug="other")
        db.add(other)
        await db.commit()
        other_id = other.id
        for _ in range(3):  # context + 3 chats = 4 exchanges in the first session
            await runtime.handle_owner_message(db, project_id, "سؤال؟")
            await runtime.wait_idle()
    assert [c for c, _ in page.commands].count("new_session") == 1 and len(page.submitted("CONTEXT")) == 1
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, "سؤال آخر؟")  # limit reached -> new session
        await runtime.wait_idle()
        await runtime.handle_owner_message(db, other_id, "سؤال عن المشروع الآخر؟")  # project changed -> new session
        await runtime.wait_idle()
        sessions = list((await db.execute(select(ArenaSession).where(ArenaSession.kind == "manager").order_by(ArenaSession.created_at))).scalars())
    assert [c for c, _ in page.commands].count("new_session") == 3 and len(page.submitted("CONTEXT")) == 3
    assert [s.is_open for s in sessions] == [False, False, True]
    assert sessions[-1].project_id == other_id
    assert any("ARENA_MANAGER_MAX_EXCHANGES=4" in (e.get("detail") or "") for e in sessions[0].events)
    assert any("project changed" in (e.get("detail") or "") for e in sessions[1].events)


async def test_owner_new_session_button_sends_a_context_prompt(env, scripted):
    make, project_id = scripted
    page, runtime = make({"*": ["جاهز."]})
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, "مرحبا؟")
        await runtime.wait_idle()
        await runtime.request_new_session(db, project_id, actor="owner@test")
        await runtime.wait_idle()
    assert [c for c, _ in page.commands].count("new_session") == 2 and len(page.submitted("CONTEXT")) == 2
    context = page.submitted("CONTEXT")[-1]
    assert "## Team" in context and "worker-1" in context and "## Open tasks" in context and "## Open pull requests" in context


async def test_manager_with_an_unfinished_plan_is_continued_up_to_the_limit(env, scripted, arena_settings, monkeypatch):
    make, project_id = scripted
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 2)
    page, runtime = make({"PLAN": ['{"summary_ar": "جزء'], "RETRY": ["ما زلت أعمل"]})
    page.continued_replies = ['{"summary_ar": "جزء ثان', '{"summary_ar": "جزء ثالث']
    page.kw_rounds = 5
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, "ابنِ دالة answer()")
        await runtime.wait_idle()
        session = (await db.execute(select(ArenaSession).where(ArenaSession.kind == "manager"))).scalars().first()
    assert [c for c, _ in page.commands].count("keep_working") == 2 and session.keep_working_count == 2
    assert any(e["kind"] == "keep_working_limit" for e in session.events)


@pytest.mark.parametrize("goal", ["مرحبا؟", "ابنِ دالة answer()"])
async def test_finished_manager_reply_gets_no_keep_working(env, scripted, arena_settings, monkeypatch, goal):
    make, project_id = scripted
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 5)
    page, runtime = make({"PLAN": [json.dumps(_plan(), ensure_ascii=False)], "*": ["تم."]})
    page.kw_rounds = 50  # Arena shows the option after every reply
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, goal)
        await runtime.wait_idle()
    assert [c for c, _ in page.commands].count("keep_working") == 0
    assert (await _jobs(env))[-1].status == "done"


async def test_manager_stops_pressing_when_keep_working_changes_nothing(env, scripted, arena_settings, monkeypatch):
    make, project_id = scripted
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 0)  # unlimited must still end
    page, runtime = make({"PLAN": ["Sure, here is my plan in words."], "RETRY": ["Still prose, sorry."]})
    page.kw_rounds = 50
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, "ابنِ دالة answer()")
        await runtime.wait_idle()
    assert [c for c, _ in page.commands].count("keep_working") == 2  # one per answer, then the JSON retry path
    assert (await _jobs(env))[-1].status == "needs_human"


async def test_stalled_manager_session_is_flagged_needs_human(env, scripted):
    make, project_id = scripted
    page, runtime = make({"*": ["تم."]})
    runtime.stall_minutes = 0.002  # ~0.1 s instead of 20 minutes
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, "مرحبا؟")
        await runtime.wait_idle()
        page.stuck = True
        runtime.poll_seconds = 0.02
        await runtime.handle_owner_message(db, project_id, "سؤال؟")
        await runtime.wait_idle()
        session = (await db.execute(select(ArenaSession).where(ArenaSession.kind == "manager"))).scalars().first()
        profile = await db.get(BrowserProfile, MANAGER_PROFILE_ID)
    assert session.phase == "needs_human" and "stalled" in session.needs_human_reason
    assert profile.connector_status == "needs_human"
    assert (await _jobs(env))[-1].status == "needs_human"
    assert ManagerArenaRuntime().stall_minutes == 20


async def test_status_question_is_answered_from_real_project_data(env, scripted):
    make, project_id = scripted
    page, runtime = make({"*": ["تم."]})
    async with env() as db:
        db.add(Task(project_id=project_id, title="Blocked login page", status="blocked", error_message="needs a human",
                    assigned_agent_id="worker-2"))
        db.add(Task(project_id=project_id, title="Add answer()", status="pr_open", pr_number=7, pr_url="https://github.com/acme/demo/pull/7",
                    assigned_agent_id="worker-1"))
        await db.commit()
        await runtime.handle_owner_message(db, project_id, "وين وصلتوا؟")
        await runtime.wait_idle()
    prompt = page.submitted("STATUS")[0]
    for needle in ("وين وصلتوا؟", "Blocked login page — blocked", "PR #7 Add answer()", "### Blockers", "needs a human"):
        assert needle in prompt, needle
    job = next(j for j in await _jobs(env) if j.kind == "status")
    assert job.status == "done" and job.context["status_summary"]["counts"]["blocked"] == 1


async def test_manager_not_connected_shows_the_arabic_message_waits_and_never_falls_back_to_demo(env, bridge, arena_settings, monkeypatch):
    _mock_mode(arena_settings, monkeypatch, bridge)
    monkeypatch.setattr(arena_settings, "AATM_MANAGER_PROVIDER", "arena_agent")
    headers = await _admin_headers(env)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/api/v1/arena/profiles/{MANAGER_PROFILE_ID}/userscript", headers=headers)
        assert r.status_code == 200
        r = await client.post("/api/v1/chat", json={"content": "مرحبا، كيف حال الفريق؟"}, headers=headers)
        assert r.status_code == 201, r.text
        reply = r.json()["manager_reply"]
        chat = (await client.get("/api/v1/chat", headers=headers)).json()
    assert reply["content"] == NOT_CONNECTED_AR == "المدير غير متصل بـ Arena — افتح ملف Chrome الخاص بالمدير"
    assert reply["metadata"]["waiting"] is True and "demo" not in reply["metadata"]
    assert not any("DEMO" in m["content"] for m in chat)
    async with env() as db:
        jobs = list((await db.execute(select(ManagerJob))).scalars())
        assert [j.status for j in jobs] == ["waiting_connection"]
        assert not list((await db.execute(select(ProviderUsageLog))).scalars()), "no model may answer instead"
        # The orchestration cycle keeps the job waiting while the profile stays disconnected.
        assert (await manager_arena_runtime.tick(db))["released"] == 0
        assert (await db.get(ManagerJob, jobs[0].id)).status == "waiting_connection"



# =========================================================================== R7.4 end to end in Chromium
async def _wait_for(check, *, timeout: float = 60.0, every: float = 0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(every)
    raise AssertionError("condition not reached in time")


@needs_chromium
async def test_manager_on_arena_end_to_end_chat_plan_review_corrections_and_owner_approval(
    env, bridge, arena_settings, monkeypatch, tmp_path
):
    """Manager and worker each in their own Chrome profile on the mock Arena Agent page, real bridge,
    real userscripts, git as GitHub's storage and a fake ``gh``. No model API is used at all."""
    from playwright.async_api import async_playwright

    from app.browser.manager import detect_playwright_capabilities
    from app.git_workflows.manager import git_workflow_manager
    from app.orchestration.arena_runner import arena_runner
    from app.orchestration.scheduler import TaskScheduler
    from app.providers.adapters.arena_agent import arena_submit_queue
    from app.runtime.agent_loop import AgentLoop
    from app.runtime.code_stream import code_stream

    _mock_mode(arena_settings, monkeypatch, bridge)
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_NEW_SESSION_URL", "")
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_MAX_KEEP_WORKING", 0)
    monkeypatch.setattr(arena_settings, "AATM_MANAGER_PROVIDER", "arena_agent")
    monkeypatch.setattr(arena_runner, "github_poll_seconds", 0)
    monkeypatch.setattr(manager_arena_runtime, "_session_factory", env)
    monkeypatch.setattr(manager_arena_runtime, "adapter", ArenaAgentAdapter())
    monkeypatch.setattr(manager_arena_runtime, "poll_seconds", 0.25)
    arena_submit_queue.history.clear()
    arena_submit_queue.max_in_flight.clear()

    repo, origin = tmp_path / "demo", tmp_path / "origin.git"
    _init_repo(repo, origin)
    gh = FakeGitHub(tmp_path, origin)
    monkeypatch.setattr(git_workflow_manager, "_exec", _passthrough(gh))
    arena = ArenaOnGitHub(tmp_path, origin, gh)
    headers = await _admin_headers(env)
    async with env() as db:
        project = Project(name="R7", slug="r7", repo_path=str(repo), repo_url=f"https://github.com/{REPO_SLUG}", default_branch="main",
                          config={"test_command": "pytest -q"})
        db.add(project)
        await db.commit()
        project_id = project.id
        worker_profile = (await db.get(Agent, "worker-1")).browser_profile_id

    model_calls: list[Any] = []
    router = _FnRouter(lambda request: model_calls.append(request) or "{}")

    async def manager_jobs(kind: str) -> list[ManagerJob]:
        async with env() as db:
            return list((await db.execute(select(ManagerJob).where(ManagerJob.kind == kind).order_by(ManagerJob.created_at))).scalars())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=180) as client:
        assert (await client.put("/api/v1/arena/agents/worker-1/mode", json={"mode": "agent"}, headers=headers)).status_code == 200
        worker_script = (await client.post(f"/api/v1/arena/profiles/{worker_profile}/userscript", headers=headers)).json()["script"]
        manager_script = (await client.post(f"/api/v1/arena/profiles/{MANAGER_PROFILE_ID}/userscript", headers=headers)).json()["script"]
        assert "profile-manager" in manager_script and worker_script != manager_script

        scheduler = TaskScheduler()
        loops = [AgentLoop(session_factory=env, provider_router=router, poll_seconds=0.01) for _ in range(2)]
        executable = detect_playwright_capabilities().get("executable_path")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
            try:
                mctx, wctx = await browser.new_context(), await browser.new_context()  # two independent Chrome profiles
                await mctx.add_init_script(manager_script)
                await wctx.add_init_script(worker_script)
                mpage, wpage = await mctx.new_page(), await wctx.new_page()
                await mpage.goto(f"{bridge.base}/mock-arena/agent/?gen_ms=500")
                await wpage.goto(f"{bridge.base}/mock-arena/agent/?gen_ms=900&kw_rounds=2&panel=1")

                # ---- 1. the manager profile connects and its Selector test passes
                await _wait_for(lambda: _connected())

                r = await client.post(f"/api/v1/arena/profiles/{MANAGER_PROFILE_ID}/command", json={"command": "selector_test"}, headers=headers)
                assert r.status_code == 200 and r.json()["result"]["ok"] is True, r.text
                ready = await manager_readiness()
                assert ready["ready"] and ready["code"] == "ready"
                health = await ArenaAgentAdapter().health_check()
                assert health.status == "healthy" and "اختبار المحددات ناجح" in health.detail
                card = (await client.get("/api/v1/arena/agent/overview", headers=headers)).json()["manager"]
                assert card["connected"] and card["selector_test"] == {"ran": True, "ok": True} and card["profile_name"] == "manager"

                # ---- 2. owner chat -> manager reply in the console (two messages: one prompt in flight)
                first = await client.post("/api/v1/chat", json={"project_id": project_id, "content": "عرّف بنفسك من فضلك؟"}, headers=headers)
                second = await client.post("/api/v1/chat", json={"project_id": project_id, "content": "ما رأيك في الفريق؟"}, headers=headers)
                assert first.status_code == second.status_code == 201
                assert first.json()["manager_reply"]["metadata"]["pending"] is True
                await _wait_for(lambda: _all_done(manager_jobs, "chat", 2), timeout=90)
                chat = (await client.get("/api/v1/chat", headers=headers)).json()
                replies = [m for m in chat if m["sender_id"] == "manager-0" and (m["metadata"] or {}).get("job_kind") == "chat"]
                assert len(replies) == 2 and all(m["content"].startswith("مرحبًا، أنا المدير") for m in replies)
                stats = await mpage.evaluate("() => window.__mockArena")
                assert [p["kind"] for p in stats["manager_prompts"]] == ["CONTEXT", "CHAT", "CHAT"]
                assert stats["new_sessions"] == 1 and stats["submits_while_generating"] == 0
                frames = code_stream.buffer("manager-0")
                assert any(f.get("path") == "manager/chat.md" and "أنا المدير" in f.get("content", "") for f in frames), \
                    "the reply must reach the manager robot's screen"

                # ---- 3. goal -> plan JSON -> tasks created and dispatched to the worker's Arena session
                await mpage.evaluate("""() => window.__mockArena.setManager({ reviews: ['request_changes', 'approve'], plan: {
                  summary_ar: 'مهمة واحدة للواجهة الخلفية.', tasks: [{ title: 'Add answer()', assignee: 'worker-1',
                  description: 'Create answer() returning 42 with a pytest test.',
                  acceptance_criteria: ['answer() returns 42', 'tests/test_answer.py tests it'], depends_on: [] }] } })""")
                goal = await client.post("/api/v1/chat", json={"project_id": project_id, "content": "ابنِ دالة answer() ترجع 42 مع اختبار"},
                                         headers=headers)
                assert goal.status_code == 201
                plan_job = (await _wait_for(lambda: _all_done(manager_jobs, "plan", 1), timeout=90))[0]
                t1_id = plan_job.result["task_ids"][0]

                async def dispatched() -> bool:  # the job is marked done just before the scheduler cycle runs
                    async with env() as db:
                        return (await db.get(Task, t1_id)).status == "in_progress"

                assert await _wait_for(dispatched, timeout=30), "the plan is dispatched through the existing scheduler"
                async with env() as db:
                    t1 = await db.get(Task, t1_id)
                    assert t1.assigned_agent_id == "worker-1" and t1.project_id == project_id
                    plan_reply = await db.get(ChatMessage, plan_job.reply_message_id)
                    assert "مهمة واحدة" in plan_reply.content and "Add answer()" in plan_reply.content

                prompts: list[str] = []

                async def _agent_on_github() -> None:
                    texts = await wpage.evaluate(
                        "() => Array.from(document.querySelectorAll('[data-message-author-role=user]')).map(e => e.textContent)")
                    for t in texts:
                        if t not in prompts:
                            prompts.append(t)
                    clicks = await wpage.evaluate("() => window.__mockArena.kw_clicks")
                    if prompts and clicks >= 2 and t1_id not in gh.prs:  # the PR comes after the agent was continued
                        arena.push(t1_id, "arena/m7a1", {"answer.py": "def answer():\n    return 42\n"}, "Add answer()")

                # ---- 4. worker PR -> manager review (request_changes) -> correction in the same worker session
                await wpage.evaluate("t => window.__mockArena.setReport(t)", _report(["answer.py", "tests/test_answer.py"], "1 passed"))

                async def _changes_requested(db) -> bool:
                    return (await db.execute(select(Task).where(Task.parent_task_id == t1_id, Task.task_type == "follow_up"))).scalars().first() is not None

                await _drive(env, scheduler, loops, _changes_requested, timeout=150, on_tick=_agent_on_github)
                assert f"[AATM-{t1_id}]" in prompts[0] and "answer() returns 42" in prompts[0], "manager criteria reach the worker"
                async with env() as db:
                    review = (await db.execute(select(Task).where(Task.parent_task_id == t1_id, Task.task_type == "review"))).scalars().first()
                    assert review.assigned_agent_id == "manager-0", "the manager reviews Arena PRs, not a reviewer employee"
                    follow_up = (await db.execute(select(Task).where(Task.parent_task_id == t1_id, Task.task_type == "follow_up"))).scalars().first()
                    assert follow_up.assigned_agent_id == "worker-1"
                stats = await mpage.evaluate("() => window.__mockArena")
                assert [p["kind"] for p in stats["manager_prompts"]][-1] == "REVIEW"
                review_job = (await manager_jobs("review"))[0]
                assert review_job.result["verdict"]["verdict"] == "request_changes" and review_job.context["diff_chars"] > 0
                assert any(c[1:3] == ["pr", "review"] and "--request-changes" in c for c in gh.calls)

                # ---- 5. corrected -> manager approves -> waits for the OWNER (nothing merges)
                await wpage.evaluate("() => window.__mockArena.setKwRounds(0)")  # the correction finishes in one go

                async def _fix_on_github() -> None:
                    await _agent_on_github()
                    if len(prompts) >= 2 and len(_git(arena.work, "log", "--oneline", "origin/main..arena/m7a1").splitlines()) < 2:
                        arena.push(t1_id, "arena/m7a1", {"tests/test_answer.py": "from answer import answer\n\n\ndef test_answer():\n"
                                                         "    assert answer() == 42\n"}, "Add test")

                async def _merge_requested(db) -> bool:
                    approvals = (await db.execute(select(ApprovalRequest).where(ApprovalRequest.action_type == "merge"))).scalars().all()
                    return any((a.payload or {}).get("task_id") == t1_id for a in approvals)

                await _drive(env, scheduler, loops, _merge_requested, timeout=150, on_tick=_fix_on_github)
                await manager_arena_runtime.wait_idle()
                assert "Add tests/test_answer.py" in prompts[1], "the manager's comments go back to the same worker session"
                wstats = await wpage.evaluate("() => window.__mockArena")
                assert wstats["new_sessions"] == 1, "corrections reuse the worker's Arena session"
                assert wstats["kw_clicks"] == 2, "no Keep working once the PR exists or the report is written"
                assert [j.result["verdict"]["verdict"] for j in await manager_jobs("review")] == ["request_changes", "approve"]
                assert not any(c[1:3] == ["pr", "merge"] for c in gh.calls), "the manager never merges"
                async with env() as db:
                    approval = next(a for a in (await db.execute(select(ApprovalRequest))).scalars() if (a.payload or {}).get("task_id") == t1_id)
                    assert approval.status == "pending" and approval.requested_by == "manager-0"
                    assert (await db.get(Task, t1_id)).status == "in_review"

                # ---- 6. one submit path for the whole team, one manager prompt in flight
                sources = {(e["profile_id"], e["source"]) for e in arena_submit_queue.history}
                assert (MANAGER_PROFILE_ID, "manager") in sources and (worker_profile, "worker") in sources
                assert arena_submit_queue.max_in_flight[MANAGER_PROFILE_ID] == 1
                mstats = await mpage.evaluate("() => window.__mockArena")
                assert mstats["submits_while_generating"] == 0 and mstats["new_sessions"] == 1
                assert model_calls == [], "no model API is used anywhere"
            finally:
                await manager_arena_runtime.wait_idle()
                await browser.close()
                for loop in loops:
                    await loop.stop()


async def _connected() -> bool:
    return bool((await manager_readiness())["connected"])


async def _all_done(jobs_of, kind: str, count: int):
    jobs = await jobs_of(kind)
    done = [j for j in jobs if j.status == "done"]
    failed = [j for j in jobs if j.status in {"failed", "needs_human"}]
    assert not failed, [(j.status, j.error) for j in failed]
    return done if len(done) >= count else None


@needs_chromium
async def test_invalid_manager_json_on_the_mock_page_retries_once_then_needs_human(env, bridge, arena_settings, monkeypatch):
    from playwright.async_api import async_playwright

    from app.browser.manager import detect_playwright_capabilities

    _mock_mode(arena_settings, monkeypatch, bridge)
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_NEW_SESSION_URL", "")
    monkeypatch.setattr(arena_settings, "AATM_MANAGER_PROVIDER", "arena_agent")
    monkeypatch.setattr(manager_arena_runtime, "_session_factory", env)
    monkeypatch.setattr(manager_arena_runtime, "adapter", ArenaAgentAdapter())
    monkeypatch.setattr(manager_arena_runtime, "poll_seconds", 0.25)
    headers = await _admin_headers(env)
    async with env() as db:
        project = Project(name="R7b", slug="r7b", repo_url=f"https://github.com/{REPO_SLUG}")
        db.add(project)
        await db.commit()
        project_id = project.id
        before = len(list((await db.execute(select(Task))).scalars()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=120) as client:
        script = (await client.post(f"/api/v1/arena/profiles/{MANAGER_PROFILE_ID}/userscript", headers=headers)).json()["script"]
        executable = detect_playwright_capabilities().get("executable_path")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
            try:
                ctx = await browser.new_context()
                await ctx.add_init_script(script)
                page = await ctx.new_page()
                await page.goto(f"{bridge.base}/mock-arena/agent/?gen_ms=300&mgr_invalid=2")
                await _wait_for(_connected)
                r = await client.post("/api/v1/chat", json={"project_id": project_id, "content": "ابنِ صفحة تسجيل دخول"}, headers=headers)
                assert r.status_code == 201
                await _wait_for(lambda: _job_in(env, "plan", {"needs_human"}), timeout=90)
                stats = await page.evaluate("() => window.__mockArena")
                assert [p["kind"] for p in stats["manager_prompts"]] == ["CONTEXT", "PLAN", "RETRY"], "exactly one retry"
                await manager_arena_runtime.wait_idle()  # the job status is saved just before its chat reply
                chat = (await client.get("/api/v1/chat", headers=headers)).json()
            finally:
                await manager_arena_runtime.wait_idle()
                await browser.close()
    reply = next(m for m in chat if (m["metadata"] or {}).get("job_kind") == "plan")
    assert reply["metadata"]["needs_human"] is True and "Sure! I would split" in reply["metadata"]["raw_reply"]
    async with env() as db:
        assert len(list((await db.execute(select(Task))).scalars())) == before, "no plan is invented"


async def _job_in(env, kind: str, statuses: set[str]):
    async with env() as db:
        jobs = list((await db.execute(select(ManagerJob).where(ManagerJob.kind == kind))).scalars())
    return [j for j in jobs if j.status in statuses] or None


# =========================================================================== R7.5 docs
def test_operations_guide_explains_running_the_manager_on_arena_with_existing_settings():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    start = doc.index("## تشغيل المدير عبر Arena")
    section = doc[start:doc.index("\n## ", start + 10)]
    for needle in ("google-chrome --user-data-dir=$HOME/.aatm-profiles/manager", "سجّل الدخول إلى Arena", "بطاقة المدير",
                   "اختبار المحددات", "AATM_MANAGER_PROVIDER=arena_agent", "الهدف الأول", NOT_CONNECTED_AR,
                   MANAGER_JSON_RETRY, "20 دقيقة", "لا يدمج أبداً", "ARENA_MANAGER_MAX_EXCHANGES"):
        assert needle in section, needle
    documented = set(re.findall(r"\b((?:ARENA|AATM)_[A-Z_]+)=", section))
    assert {"AATM_MANAGER_PROVIDER", "ARENA_MANAGER_MAX_EXCHANGES"} <= documented
    assert documented - set(Settings.model_fields) == set()
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    assert "AATM_MANAGER_PROVIDER=" in env_example and "ARENA_MANAGER_MAX_EXCHANGES=" in env_example


async def test_project_team_limits_who_the_manager_may_plan_for(env, scripted):
    make, _ = scripted
    w2 = '```json\n{"summary_ar": "خطة", "tasks": [{"title": "UI", "assignee": "worker-2", "description": "d", "acceptance_criteria": ["c"], "depends_on": []}]}\n```'
    w1 = '```json\n{"summary_ar": "خطة", "tasks": [{"title": "API", "assignee": "worker-1", "description": "d", "acceptance_criteria": ["c"], "depends_on": []}]}\n```'
    page, runtime = make({"PLAN": [w2], "RETRY": [w1]})
    async with env() as db:
        project = Project(name="Team test", slug="team-test", repo_url="https://github.com/acme/team", config={"team": ["worker-1"]})
        db.add(project)
        await db.commit()
        await runtime.handle_owner_message(db, project.id, "ابنِ واجهة وخادمًا")
    await runtime.wait_idle()
    plan_prompt = page.submitted("PLAN")[0]
    assert "worker-1 —" in plan_prompt and "worker-2 —" not in plan_prompt and "worker-3 —" not in plan_prompt
    job = next(j for j in await _jobs(env) if j.kind == "plan")
    assert job.status == "done" and job.json_retries == 1, "worker-2 is not on this project's team"
    async with env() as db:
        assert (await db.get(Task, job.result["task_ids"][0])).assigned_agent_id == "worker-1"
        # Without a team the manager still sees every enabled worker.
        everyone = await runtime._team(db, None)
    assert {"worker-1", "worker-2"} <= {m["id"] for m in everyone}
