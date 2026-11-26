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
    MANAGER_JSON_RETRY_DETAIL,
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
    REVIEW_PROMPT_MAX_CHARS,
    ManagerArenaRuntime,
    classify_owner_message,
    manager_arena_runtime,
    manager_provider_mode,
    manager_readiness,
)
from app.providers.adapters.arena_agent import ArenaAgentAdapter, ArenaAgentError


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
    for needle in ("[AATM-MANAGER:PLAN]", "manager-0", "worker-1 — Backend: FastAPI", "Workers never merge", "[AATM-<task_id>]",
                   '"acceptance_criteria"', "ONLY one JSON block", "acme/demo"):
        assert needle in plan, needle
    task = Task(id="t-1", title="Add answer()", description="answer() returns 42", assigned_agent_id="worker-1")
    big = "+x\n" * 20_000
    shown, truncated = truncate_diff(big, url="https://github.com/acme/demo/pull/1")
    assert truncated and "DIFF TRUNCATED" in shown and "pull/1" in shown and len(shown) < len(big)
    review = build_manager_review_prompt(task, acceptance=["answer() returns 42"], report="## AATM REPORT", diff=big,
                                         pr_url="https://github.com/acme/demo/pull/1", round_number=1, project=project, team=team)
    for needle in ("[AATM-MANAGER:REVIEW]", "answer() returns 42", "AATM REPORT", "DIFF TRUNCATED", '"verdict"',
                   "merge the pull request yourself"):
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
        self.pre_busy_ticks = 0
        self.busy_submits = 0
        self.submit_error: Exception | None = None
        self.kw_rounds = 0
        self.stuck = False
        self.commands: list[tuple[str, str | None]] = []
        self.reply = ""
        self.continued_replies: list[str] = []
        self.last_kind = "CHAT"

    def _move(self, page: str) -> None:
        self.page, self.since = page, self.since + 1

    async def state(self, profile_id: str) -> dict[str, Any]:
        if self.pre_busy_ticks > 0:
            self.pre_busy_ticks -= 1
            return {"ok": True, "connected": True, "mode": "agent", "status": "ready", "agent_state": "generating",
                    "agent_state_since": str(self.since)}
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
            if self.submit_error is not None:
                raise self.submit_error
            if self.busy_submits > 0:
                self.busy_submits -= 1
                raise ArenaAgentError("busy", "the agent is still generating")
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
        runtime.busy_poll_seconds = 0.01
        runtime.retry_backoff_seconds = 0
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
    assert page.submitted("PLAN") and sum(1 for t in page.submitted() if t.startswith(MANAGER_JSON_RETRY)) == 1
    retry = next(t for t in page.submitted() if t.startswith(MANAGER_JSON_RETRY))
    assert '"summary_ar": ""' in retry and '"tasks": []' in retry, "the retry carries an empty JSON template (E33)"
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
    runtime.stall_minutes = 0.01  # ~0.6 s instead of 20 minutes (0.12 s was too tight under parallel load)
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


async def test_a_clarifying_question_is_shown_to_the_owner_not_treated_as_invalid_json(env, scripted):
    """The manager answers a vague goal with {"ambiguous": true, "tasks": []} and a question in summary_ar.
    That is a valid answer: show the question, create no tasks and do not send the JSON retry."""
    make, project_id = scripted
    question = "ما نوع البرنامج المطلوب: ويب أم سطح مكتب؟ وكم لعبة؟"
    reply_json = '```json\n{"ambiguous": true, "summary_ar": "%s", "tasks": []}\n```' % question
    page, runtime = make({"PLAN": [reply_json], "RETRY": ["must never be asked"]})
    async with env() as db:
        before = len(list((await db.execute(select(Task))).scalars()))
        await runtime.handle_owner_message(db, project_id, "اكتب لي برنامج العاب تجربة")
    await runtime.wait_idle()
    job = next(j for j in await _jobs(env) if j.kind == "plan")
    assert job.status == "done" and job.error is None and job.json_retries == 0
    assert (job.result or {}).get("clarification") is True
    assert page.submitted().count(MANAGER_JSON_RETRY_DETAIL) == 0, "a question must not trigger the JSON retry"
    async with env() as db:
        assert len(list((await db.execute(select(Task))).scalars())) == before, "a question creates no tasks"
        replies = list((await db.execute(select(ChatMessage).where(ChatMessage.sender_id == "manager-0"))).scalars())
    assert any(question in m.content for m in replies), "the owner must see the manager's question"
    assert not any("تعذّر على المدير" in m.content for m in replies)


async def test_ambiguous_flag_without_a_question_or_with_tasks_is_not_a_clarification(env, scripted):
    """Only an ambiguous reply with a question and no tasks counts; anything else keeps the old strict rules."""
    from app.orchestration.manager_arena import clarification_question

    assert clarification_question({"ambiguous": True, "summary_ar": "سؤال؟", "tasks": []}) == "سؤال؟"
    assert clarification_question({"ambiguous": True, "summary_ar": "  ", "tasks": []}) is None
    assert clarification_question({"ambiguous": True, "summary_ar": "سؤال؟", "tasks": [{"title": "x"}]}) is None
    assert clarification_question({"ambiguous": False, "summary_ar": "سؤال؟", "tasks": []}) is None
    assert clarification_question({"summary_ar": "سؤال؟", "tasks": []}) is None
    assert clarification_question(None) is None


async def test_manager_gets_a_new_session_and_context_when_the_team_changes(env, scripted):
    """The context (project + team) is sent once per session. Adding a worker to the project's team mid-session
    must start a new session so the manager plans for the team the owner actually has."""
    from app.models import Agent

    make, project_id = scripted
    page, runtime = make({"*": ["تم."]})
    async with env() as db:
        project = await db.get(Project, project_id)
        project.config = {"team": ["worker-1"]}
        await db.commit()
        await runtime.handle_owner_message(db, project_id, "ما حالة الفريق؟")
    await runtime.wait_idle()
    contexts = [t for t in page.submitted() if "[AATM-MANAGER:CONTEXT]" in t]
    assert len(contexts) == 1 and "worker-2" not in contexts[0]

    async with env() as db:  # same session, same team: no new context
        await runtime.handle_owner_message(db, project_id, "ما حالة الفريق الآن؟")
    await runtime.wait_idle()
    assert len([t for t in page.submitted() if "[AATM-MANAGER:CONTEXT]" in t]) == 1

    async with env() as db:  # the owner adds workers 2 and 3
        project = await db.get(Project, project_id)
        project.config = {"team": ["worker-1", "worker-2", "worker-3"]}
        assert await db.get(Agent, "worker-2") is not None
        await db.commit()
        await runtime.handle_owner_message(db, project_id, "وزع العمل على الفريق")
    await runtime.wait_idle()
    contexts = [t for t in page.submitted() if "[AATM-MANAGER:CONTEXT]" in t]
    assert len(contexts) == 2 and "worker-2" in contexts[1] and "worker-3" in contexts[1]


# =========================================================================== E37 / E33 / E23 / E26
async def test_busy_manager_waits_for_idle_without_spending_an_attempt(env, scripted):
    """E37: two jobs meet a busy window, it becomes idle, both finish with one attempt each and no needs_human."""
    make, project_id = scripted
    page, runtime = make({"*": ["تم."]})
    page.busy_submits = 2  # the context submit and the first prompt both meet a busy window
    runtime.busy_wait_seconds = 1.0
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, "سؤال أول؟")
        await runtime.handle_owner_message(db, project_id, "سؤال ثانٍ؟")
    await runtime.wait_idle()
    jobs = await _jobs(env)
    assert [j.status for j in jobs] == ["done", "done"], [(j.status, j.error) for j in jobs]
    assert [j.attempts for j in jobs] == [1, 1], "a busy window must not spend an attempt"
    assert not any(j.status == "needs_human" for j in jobs)
    async with env() as db:
        messages = list((await db.execute(select(ChatMessage).where(ChatMessage.sender_id == "manager-0"))).scalars())
    assert any((m.metadata_json or {}).get("waiting_for_manager") is True for m in messages), \
        "the owner is told that the manager is busy instead of seeing a failure"


async def test_real_failure_still_stops_after_three_attempts(env, scripted):
    """E37: MAX_JOB_ATTEMPTS stays 3 for a real (non-busy) failure."""
    make, project_id = scripted
    page, runtime = make({"*": ["تم."]})
    page.submit_error = ArenaAgentError("offline", "the page went away")
    runtime.retry_backoff_seconds = 0
    async with env() as db:
        job = await runtime.enqueue(db, "chat", project_id=project_id, text="مرحبا")
        job_id = job.id
        for _ in range(3):
            await runtime._run_job(db, job)
        job = await db.get(ManagerJob, job_id)
        assert job.attempts == 3 and job.status == "waiting_connection"
        # 2026-11-26: three failed sends first get one fresh Arena chat, then three more attempts.
        result = await runtime._run_job(db, job)
        assert result.get("fresh_session_retry") is True and job.attempts == 0 and job.status == "queued"
        assert job.context["fresh_session_retry"] is True
        for _ in range(3):
            await runtime._run_job(db, job)
        result = await runtime._run_job(db, job)
    assert result["status"] == "failed" and "stopped after 3 attempts" in job.error


def test_waiting_for_manager_text_names_the_review():
    from app.orchestration.manager_arena import _waiting_for_manager_text

    assert "المراجعة تنتظر المدير" in _waiting_for_manager_text("review")
    assert "بانتظار" in _waiting_for_manager_text("chat")


def test_review_prose_verdict_is_extracted_without_inventing_one():
    from app.orchestration.manager_arena import review_verdict_from_text

    approve = review_verdict_from_text("The change looks correct. I approve it; the tests cover the new path.")
    assert approve and approve["verdict"] == "approve"
    changes = review_verdict_from_text("Verdict: request_changes\n- The auth check is missing on /login.")
    assert changes and changes["verdict"] == "request_changes"
    assert any("auth" in c for c in changes["comments"])
    assert review_verdict_from_text("Here is a summary of the files.") is None
    assert review_verdict_from_text("") is None


async def test_review_without_json_retries_with_a_template_then_reads_the_prose_verdict(env, scripted, monkeypatch):
    """E33: no JSON -> retry with an empty JSON template -> still no JSON -> verdict from the prose."""
    from app.orchestration import arena_runner as arena_runner_module
    from app.orchestration import review as review_module
    from app.orchestration import scheduler as scheduler_module
    from app.runtime import code_stream as code_stream_module

    make, project_id = scripted
    page, runtime = make({
        "REVIEW": ["I cannot approve this PR: the login check is missing."],
        "RETRY": ["Verdict: request_changes. Add the missing auth token check before approving."],
    })

    async def material(db, parent):
        return {"changed_files": [{"filename": "app/auth.py", "status": "modified", "additions": 10, "deletions": 2}],
                "mechanical_discrepancies": []}

    async def compare(project, session):
        return {"ok": True, "files": [{"filename": "app/auth.py", "status": "modified", "additions": 10, "deletions": 2,
                                       "patch": "+def login():\n+    check_token(token)\n"}]}

    submitted: list[dict[str, Any]] = []

    async def submit_review(db, review, manager, *, event, body):
        submitted.append({"event": event, "body": body})
        return {"follow_up_task_id": None, "merge_approval_id": None, "event": event}

    async def complete_task(db, task, *, summary, target_status):
        task.status = target_status

    async def publish(frame):
        return None

    monkeypatch.setattr(arena_runner_module.arena_runner, "review_material", material)
    monkeypatch.setattr(arena_runner_module.arena_runner, "_compare", compare)
    monkeypatch.setattr(review_module.review_service, "submit_review", submit_review)
    monkeypatch.setattr(scheduler_module.task_scheduler, "complete_task", complete_task)
    monkeypatch.setattr(code_stream_module.code_stream, "publish", publish)

    async with env() as db:
        parent = Task(project_id=project_id, title="Add login", status="pr_open", task_type="implementation",
                      assigned_agent_id="worker-1", pr_url="https://github.com/acme/demo/pull/7", pr_number=7)
        db.add(parent)
        await db.flush()
        review = Task(project_id=project_id, title="Review login", status="in_progress", task_type="review",
                      assigned_agent_id="manager-0", parent_task_id=parent.id)
        db.add(review)
        db.add(ArenaSession(kind="worker", agent_id="worker-1", profile_id="worker-1", task_id=parent.id,
                            project_id=project_id, phase="awaiting_approval", page_state="idle",
                            report="## AATM REPORT\nImplementation summary: done.", is_open=True))
        await db.commit()
        job = await runtime.enqueue(db, "review", project_id=project_id, review_task_id=review.id)
        job_id = job.id
    async with env() as db:
        job = await db.get(ManagerJob, job_id)
        result = await runtime._run_job(db, job)
        job = await db.get(ManagerJob, job_id)
        review = await db.get(Task, review.id)
    assert result["status"] == "done" and job.status == "done", (job.status, job.error)
    assert job.json_retries == 1
    assert job.result["verdict"]["verdict"] == "request_changes"
    assert review.status == "completed"
    assert submitted and submitted[0]["event"] == "REQUEST_CHANGES"
    retry = next(t for t in page.submitted() if t.startswith(MANAGER_JSON_RETRY))
    assert '"verdict": ""' in retry and '"comments": []' in retry, "the retry carries an empty JSON template"
    async with env() as db:
        assert not list((await db.execute(select(ChatMessage).where(
            ChatMessage.content.contains("تعذّر على المدير")))).scalars())


async def test_empty_tasks_reply_shows_summary_ar_to_the_owner(env, scripted):
    """E26: tasks=[] (a refusal, with or without ambiguous) is the manager's answer, not an error."""
    make, project_id = scripted
    summary = "أعتذر، هذا الطلب خارج نطاق العمل الحالي ولن أنشئ له أي مهام."
    reply_json = '```json\n{"summary_ar": "%s", "tasks": []}\n```' % summary
    page, runtime = make({"PLAN": [reply_json], "RETRY": ["must never be asked"]})
    async with env() as db:
        before = len(list((await db.execute(select(Task))).scalars()))
        await runtime.handle_owner_message(db, project_id, "اكتب لي تقريرًا عن الطقس غدًا")
    await runtime.wait_idle()
    job = next(j for j in await _jobs(env) if j.kind == "plan")
    assert job.status == "done" and job.error is None and job.json_retries == 0
    assert (job.result or {}).get("empty_tasks") is True
    assert sum(1 for t in page.submitted() if t.startswith(MANAGER_JSON_RETRY)) == 0, "no retry for an empty-tasks reply"
    async with env() as db:
        message = await db.get(ChatMessage, job.reply_message_id)
        assert len(list((await db.execute(select(Task))).scalars())) == before, "no task is created"
        assert not list((await db.execute(select(ChatMessage).where(
            ChatMessage.content.contains("تعذّر على المدير")))).scalars())
    assert message.content == summary, "the owner sees summary_ar exactly as the manager wrote it"


def _large_pr_files() -> list[dict[str, Any]]:
    files = [{"filename": f"src/module_{i}.py", "status": "modified", "additions": 250, "deletions": 10,
              "patch": "\n".join(f"+value_{j} = compute({j})" for j in range(250))} for i in range(17)]
    files += [
        {"filename": "backend/app/api/auth.py", "status": "modified", "additions": 40, "deletions": 2,
         "patch": "+def login(user):\n+    check_token(TOKEN)\n+    return session(user)\n" + "+pass\n" * 37},
        {"filename": "backend/alembic/versions/0001_init.py", "status": "added", "additions": 30, "deletions": 0,
         "patch": "+def upgrade():\n+    op.create_table('users')\n" + "+pass\n" * 28},
        {"filename": "backend/app/old_module.py", "status": "removed", "additions": 0, "deletions": 20,
         "patch": "-def old():\n-    pass\n" + "-pass\n" * 18},
    ]
    return files


def test_large_pr_review_prompt_is_summarized_capped_and_includes_risky_parts():
    """E23: 20 files and +4000 lines -> under the cap, every file in the summary, risky hunks included."""
    project = Project(name="Demo", slug="demo", repo_url="https://github.com/acme/demo", default_branch="main")
    team = [{"id": "worker-1", "name": "Backend", "specialty": "FastAPI", "skills": ["python"]}]
    task = Task(id="t-big", title="Add login", description="login + migration", assigned_agent_id="worker-1")
    files = _large_pr_files()
    assert len(files) == 20 and sum(f["additions"] for f in files) > 4000
    diff = "\n".join(f"--- {f['filename']}\n{f['patch']}" for f in files)
    prompt = build_manager_review_prompt(task, acceptance=["login works"], report="## AATM REPORT", diff=diff,
                                         pr_url="https://github.com/acme/demo/pull/7", round_number=1,
                                         project=project, team=team, files=files)
    assert len(prompt) <= REVIEW_PROMPT_MAX_CHARS, len(prompt)
    for item in files:
        assert item["filename"] in prompt, item["filename"]
    assert "check_token(TOKEN)" in prompt, "the auth hunk is included"
    assert "create_table('users')" in prompt, "the migration hunk is included"
    assert "def old():" in prompt, "the deleted file is included"
    assert "DIFF TRUNCATED" in prompt, "the full diff is not sent"


def test_docs_only_pr_skips_the_line_by_line_review():
    project = Project(name="Demo", slug="demo", repo_url="https://github.com/acme/demo", default_branch="main")
    team = [{"id": "worker-1", "name": "Backend", "specialty": "FastAPI", "skills": []}]
    task = Task(id="t-docs", title="Update docs", description="docs", assigned_agent_id="worker-1")
    files = [
        {"filename": "docs/guide.md", "status": "modified", "additions": 40, "deletions": 2,
         "patch": "+# Guide\n+Use the new endpoint.\n" + "+line\n" * 38},
        {"filename": "README.md", "status": "modified", "additions": 10, "deletions": 0,
         "patch": "+# Readme\n+line\n" * 5},
    ]
    diff = "\n".join(f"--- {f['filename']}\n{f['patch']}" for f in files)
    prompt = build_manager_review_prompt(task, acceptance=["docs updated"], report="## AATM REPORT", diff=diff,
                                         pr_url="https://github.com/acme/demo/pull/8", round_number=1,
                                         project=project, team=team, files=files)
    assert "Documentation-only change" in prompt
    assert "Use the new endpoint" not in prompt, "a docs-only change is not reviewed line by line"
    for item in files:
        assert item["filename"] in prompt


async def test_busy_timeout_never_counts_as_an_attempt(env, scripted):
    """E37: when the window stays busy past the timeout the job goes back to the queue with attempts intact."""
    make, project_id = scripted
    page, runtime = make({"*": ["تم."]})
    page.busy_submits = 10_000
    runtime.busy_wait_seconds = 0.05
    runtime.busy_poll_seconds = 0.01
    async with env() as db:
        job = await runtime.enqueue(db, "chat", project_id=project_id, text="مرحبا")
        job_id = job.id
        result = await runtime._run_job(db, job)
        job = await db.get(ManagerJob, job_id)
    assert result["status"] == "queued" and job.status == "queued"
    assert job.attempts == 0, "a busy window must not spend an attempt"
    assert "بانتظار المدير" in (job.error or "")
    assert all(j.status != "needs_human" for j in await _jobs(env))


def test_a_review_over_the_part_limit_is_split_and_every_file_is_still_covered():
    project = Project(name="Demo", slug="demo", repo_url="https://github.com/acme/demo", default_branch="main")
    team = [{"id": "worker-1", "name": "Backend", "specialty": "FastAPI", "skills": []}]
    task = Task(id="t-parts", title="Many files", description="many", assigned_agent_id="worker-1")
    files = [{"filename": f"src/part_file_{i}.py", "status": "modified", "additions": 5, "deletions": 1,
              "patch": f"+value_{i} = {i}\n"} for i in range(45)]
    diff = "\n".join(f"--- {f['filename']}\n{f['patch']}" for f in files)
    prompts = [build_manager_review_prompt(task, acceptance=["all files"], report="## AATM REPORT", diff=diff,
                                           pr_url="https://github.com/acme/demo/pull/9", round_number=1,
                                           project=project, team=team, files=files, part=index, parts=2)
               for index in range(2)]
    assert len(prompts) == 2 and all(len(p) <= REVIEW_PROMPT_MAX_CHARS for p in prompts)
    assert "review part 1 of 2" in prompts[0] and "review part 2 of 2" in prompts[1]
    combined = "\n".join(prompts)
    for item in files:
        assert item["filename"] in combined, item["filename"]


async def test_a_generating_window_is_awaited_before_the_prompt_is_sent(env, scripted):
    """E37: the page is still generating when the job starts; the prompt waits for idle instead of failing."""
    make, project_id = scripted
    page, runtime = make({"*": ["تم."]})
    async with env() as db:  # create the manager session first, so no new_session consumes the busy ticks
        await runtime.handle_owner_message(db, project_id, "مرحبا؟")
    await runtime.wait_idle()
    page.pre_busy_ticks = 2
    runtime.busy_wait_seconds = 1.0
    async with env() as db:
        await runtime.handle_owner_message(db, project_id, "سؤال؟")
    await runtime.wait_idle()
    job = (await _jobs(env))[-1]
    assert job.status == "done" and job.attempts == 1 and job.error is None
    async with env() as db:
        messages = list((await db.execute(select(ChatMessage).where(ChatMessage.sender_id == "manager-0"))).scalars())
    assert any((m.metadata_json or {}).get("waiting_for_manager") is True for m in messages)


async def test_a_review_blocked_only_on_an_unreadable_reply_is_retried_once(env, scripted, monkeypatch):
    """Live 2026-11-26 (PR #6): our own JSON retry was read as the reply; the PR was never judged."""
    from app.db.base import utcnow
    from app.orchestration import manager_arena as module

    make, project_id = scripted
    _page, runtime = make()
    monkeypatch.setattr(module, "UNREADABLE_RETRY_WAIT_SECONDS", 0)
    async with env() as db:
        parent = Task(project_id=project_id, title="UI", status="in_review", assigned_agent_id="worker-2", pr_number=6)
        db.add(parent)
        await db.flush()
        review = Task(project_id=project_id, title="Review PR #6", status="blocked", task_type="review",
                      assigned_agent_id="manager-0", parent_task_id=parent.id,
                      error_message="Manager review needs a human: invalid JSON after one retry: verdict must be ...")
        other = Task(project_id=project_id, title="Review PR #7", status="blocked", task_type="review",
                     assigned_agent_id="manager-0", parent_task_id=parent.id,
                     error_message="Manager review needs a human: the review reply names PR #3")
        db.add_all([review, other])
        await db.flush()
        for task in (review, other):
            db.add(ManagerJob(kind="review", status="needs_human", project_id=project_id, review_task_id=task.id,
                              finished_at=utcnow()))
        await db.commit()
        assert await runtime._retry_unreadable_reviews(db) == 1
        assert review.status == "in_progress" and other.status == "blocked"
        review.status = "blocked"
        review.error_message = "Manager review needs a human: invalid JSON after one retry"
        for job in (await db.execute(select(ManagerJob).where(ManagerJob.review_task_id == review.id))).scalars():
            job.status = "needs_human"
        await db.commit()
        assert await runtime._retry_unreadable_reviews(db) == 0, "only one automatic retry"


async def test_a_needs_human_review_job_is_closed_once_a_newer_job_finished(env, scripted):
    """Live 2026-11-26 (PR #6): job 2bc8e558 stayed needs_human after retry f9329064 gave the verdict."""
    from datetime import timedelta

    from app.db.base import utcnow

    make, project_id = scripted
    _page, runtime = make()
    async with env() as db:
        review = Task(project_id=project_id, title="Review PR #6", status="done", task_type="review",
                      assigned_agent_id="manager-0")
        lonely = Task(project_id=project_id, title="Review PR #7", status="blocked", task_type="review",
                      assigned_agent_id="manager-0")
        db.add_all([review, lonely])
        await db.flush()
        start = utcnow() - timedelta(hours=1)
        old = ManagerJob(kind="review", status="needs_human", project_id=project_id, review_task_id=review.id,
                         error="invalid JSON after one retry", created_at=start)
        new = ManagerJob(kind="review", status="done", project_id=project_id, review_task_id=review.id,
                         created_at=start + timedelta(minutes=30))
        alone = ManagerJob(kind="review", status="needs_human", project_id=project_id, review_task_id=lonely.id,
                           created_at=start)
        db.add_all([old, new, alone])
        await db.commit()
        assert await runtime._close_superseded_review_jobs(db) == 1
        assert old.status == "cancelled" and "superseded" in old.error
        assert alone.status == "needs_human", "a job with no finished successor still waits for the owner"
