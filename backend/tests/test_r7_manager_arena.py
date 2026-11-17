"""R7 — the manager (manager-0) on Arena Agent mode, like the workers.

R7.2: the ``Arena Agent (browser)`` wizard option, ``AATM_MANAGER_PROVIDER`` and the provider card's
health, which is healthy only when the manager's own Arena profile is connected and its Selector
test passed (otherwise it says why, in Arabic).
"""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_d_arena_connector
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from test_d_arena_connector import (  # noqa: F401 - fixtures
    _admin_headers,
    arena_settings,
    bridge,
    env,
)

from app.browser.arena_connector import MANAGER_PROFILE_ID
from app.core.config import Settings
from app.main import app
from app.models import Agent, AIProvider, BrowserProfile, ChatMessage, SystemSetting, Task
from app.orchestration.manager_arena import (
    MANAGER_PROVIDER_SETTING,
    NOT_CONNECTED_AR,
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
