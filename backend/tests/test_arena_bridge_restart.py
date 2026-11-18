"""A bridge restart must not leave the manager and workers unregistered, and goals must be routed safely."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_d_arena_connector
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import httpx
import pytest
import websockets
from bridge.connector import ConnectorHub, ProfileRegistration
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_d_arena_connector import (  # noqa: F401 - fixtures
    BRIDGE_TOKEN,
    _admin_headers,
    arena_settings,
    bridge,
    env,
)

from app.browser import arena_connector
from app.browser.arena_connector import MANAGER_PROFILE_ID
from app.browser.arena_sessions import sync_profiles_with_bridge
from app.main import app
from app.models import Agent, BrowserProfile, ManagerJob, Project
from app.orchestration.manager_arena import classify_owner_message, manager_arena_runtime, manager_readiness


def _mock_mode(arena_settings, monkeypatch, bridge) -> None:
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", f"{bridge.base}/mock-arena/")
    monkeypatch.setattr(arena_settings, "ARENA_BRIDGE_URL", bridge.base)


async def _hello(bridge, profile_id: str, token: str) -> int | None:
    url = f"ws://127.0.0.1:{bridge.port}/v1/connector"
    async with websockets.connect(url, origin=bridge.base) as ws:
        await ws.send(json.dumps({"type": "hello", "profile_id": profile_id, "token": token, "page_origin": bridge.base}))
        try:
            await ws.recv()
        except websockets.ConnectionClosed as closed:
            return closed.rcvd.code if closed.rcvd else None
    return None


async def test_enabled_profiles_are_registered_again_after_a_bridge_restart(env, bridge, arena_settings, monkeypatch):
    _mock_mode(arena_settings, monkeypatch, bridge)
    async with env() as db:
        profiles = list((await db.execute(select(BrowserProfile))).scalars())
        for profile in profiles:
            profile.connector_token_hash = hashlib.sha256(profile.id.encode()).hexdigest()
        disabled = next(p for p in profiles if p.id != MANAGER_PROFILE_ID and p.agent_id)
        (await db.get(Agent, disabled.agent_id)).is_enabled = False
        await db.commit()
        hashes = {p.id: p.connector_token_hash for p in profiles}

        assert (await manager_readiness())["code"] == "not_registered"
        registered = await sync_profiles_with_bridge(db)

        assert MANAGER_PROFILE_ID in registered
        assert disabled.id not in registered and disabled.id not in bridge.hub.profiles
        assert (await manager_readiness())["code"] == "offline"
        assert bridge.hub.profiles[MANAGER_PROFILE_ID].token_hash == hashes[MANAGER_PROFILE_ID]
        assert await sync_profiles_with_bridge(db) == [], "known profiles are left alone"


async def test_unregistered_page_may_retry_but_a_wrong_token_may_not(bridge):
    assert await _hello(bridge, "p1", "token") == 4404, "installed scripts retry on this code"
    registration = {"token_sha256": hashlib.sha256(b"token").hexdigest(), "agent_id": "worker-1",
                    "allowed_origins": [bridge.base]}
    async with httpx.AsyncClient(base_url=bridge.base, headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"}) as http:
        assert (await http.put("/v1/connector/profiles/p1", json=registration)).status_code == 200
    assert await _hello(bridge, "p1", "wrong") == 4401


@pytest.mark.parametrize("word", ["status", "progress"])
def test_long_brief_mentioning_status_is_a_goal_but_a_short_question_is_not(word):
    assert classify_owner_message(f"What is the {word}")[0] == "status"
    assert classify_owner_message(f"Build the dashboard. Show each worker's {word}. " + "Details. " * 40)[0] == "plan"


async def test_goal_for_a_completed_project_is_refused_and_never_waits_for_arena(env, arena_settings):
    async with env() as db:
        project = Project(name="finished", slug="finished", status="completed")
        db.add(project)
        await db.commit()
        reply = await manager_arena_runtime.handle_owner_message(db, project.id, "Build a new page")
        job = (await db.execute(select(ManagerJob))).scalars().one()
    assert job.status == "failed" and job.error == "project is completed"
    assert "finished" in reply.content and reply.metadata_json.get("failed") is True


async def test_status_question_about_a_completed_project_is_still_allowed(env, arena_settings):
    headers = await _admin_headers(env)
    async with env() as db:
        project = Project(name="finished", slug="finished", status="completed")
        db.add(project)
        await db.commit()
        project_id = project.id
    arena_settings.AATM_MANAGER_PROVIDER = "arena_agent"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/chat", json={"project_id": project_id, "content": "status"}, headers=headers)
    finally:
        arena_settings.AATM_MANAGER_PROVIDER = ""
    assert response.status_code == 201, response.text
    async with env() as db:
        assert (await db.execute(select(ManagerJob))).scalars().one().kind == "status"


def _registration(token: str = "secret-token") -> ProfileRegistration:
    return ProfileRegistration(token_sha256=hashlib.sha256(token.encode()).hexdigest(), agent_id="worker-1",
                               allowed_origins=["http://127.0.0.1:4100"], mode="agent")


def test_restarted_bridge_accepts_the_installed_token_without_the_backend(tmp_path):
    state_file = tmp_path / "private" / "connector-profiles.json"
    ConnectorHub(state_file).register("p1", _registration())

    restored = ConnectorHub(state_file)
    hello = {"type": "hello", "profile_id": "p1", "token": "secret-token", "page_origin": "http://127.0.0.1:4100"}

    assert restored._authenticate(hello, "http://127.0.0.1:4100") is restored.profiles["p1"]
    assert restored.profiles["p1"].agent_id == "worker-1" and restored.profiles["p1"].mode == "agent"
    assert "secret-token" not in state_file.read_text()
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600


def test_human_check_survives_a_bridge_restart_until_a_person_resumes(tmp_path):
    state_file = tmp_path / "connector-profiles.json"
    hub = ConnectorHub(state_file)
    hub._mark_needs_human(hub.register("p1", _registration()), "page shows: rate limit")

    restored = ConnectorHub(state_file)
    assert restored.profiles["p1"].status == "needs_human"
    assert restored.profiles["p1"].needs_human_reason == "page shows: rate limit"

    restored.reset("p1")
    assert ConnectorHub(state_file).profiles["p1"].needs_human_reason is None


def test_unregistered_profile_stays_gone_and_a_broken_state_file_is_ignored(tmp_path):
    state_file = tmp_path / "connector-profiles.json"
    hub = ConnectorHub(state_file)
    hub.register("p1", _registration())
    hub.profiles.pop("p1")
    hub._save()
    assert ConnectorHub(state_file).profiles == {}
    state_file.write_text("{not json")
    assert ConnectorHub(state_file).profiles == {}


def test_userscript_keeps_retrying_after_every_close_code_and_start_enables_persistence():
    source = Path(arena_connector.__file__).read_text()
    assert "if (closed) return;\n      // Never give up" in source
    assert "rejected ? 60000 : retryMs" in source
    assert "ev.code === 4410) return" not in source
    assert "ev.code === 4409 ? 300000" in source  # two tabs of one profile must not keep replacing each other
    start = (Path(__file__).resolve().parents[2] / "scripts/ubuntu/start.sh").read_text()
    assert "ARENA_CONNECTOR_STATE_FILE" in start and "$HOME/.aatm/bridge/" in start
