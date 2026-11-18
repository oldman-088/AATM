"""R1 — Arena full integration: no quotas, every profile, independent sessions, recovery.

* the temporary authorization system and every usage restriction are gone (settings, bridge, code);
* the bridge drives any number of Arena Agent profiles independently through validated commands;
* a human check stops only that profile (read-only commands still work) until a human resumes it;
* a browser disconnect fails in-flight commands as ``offline`` and the session recovers on reconnect;
* the autoscaler hands Arena Agent mode to new workers without a profile-count ceiling;
* each employee owns its session; a merged task's session is never reused.
"""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_d_arena_connector
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest
import websockets
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_d_arena_connector import (  # noqa: F401 - fixtures
    BRIDGE_TOKEN,
    _admin_headers,
    arena_settings,
    bridge,
    env,
)

from app.browser.arena_sessions import (
    ArenaSessionConflict,
    apply_bridge_state,
    claim_session,
    close_session,
    is_arena_agent_worker,
)
from app.main import app
from app.models import Agent, ArenaSession, BrowserProfile, Project, Task
from app.orchestration.autoscale import autoscale_service

ROOT = Path(__file__).resolve().parents[2]
ADMIN = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}


def _reg(token: str, origin: str, **extra) -> dict:
    return {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "agent_id": None,
            "allowed_origins": [origin], "mode": "agent", **extra}


class FakeAgentPage:
    """Stands in for the userscript on one employee's Arena Agent tab."""

    def __init__(self, bridge, profile_id: str, token: str) -> None:
        self.bridge, self.profile_id, self.token = bridge, profile_id, token
        self.ws = None
        self.commands: list[dict] = []
        self.task: asyncio.Task | None = None
        self.hold = asyncio.Event()

    async def connect(self) -> dict:
        self.ws = await websockets.connect(f"ws://127.0.0.1:{self.bridge.port}/v1/connector", origin=self.bridge.base)
        await self.ws.send(json.dumps({"type": "hello", "profile_id": self.profile_id, "token": self.token,
                                       "page_origin": self.bridge.base, "script_version": "2.0.0"}))
        config = json.loads(await self.ws.recv())
        self.task = asyncio.create_task(self._serve())
        return config

    async def state(self, state: str, reason: str | None = None) -> None:
        await self.ws.send(json.dumps({"type": "state", "state": state, "reason": reason}))

    async def _serve(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("type") != "command":
                    continue
                self.commands.append(msg)
                if msg["command"] == "submit" and "HANG" in msg["args"]["text"]:
                    continue  # never answers: used to test disconnect recovery
                data = {"command": msg["command"], "profile": self.profile_id}
                if msg["command"] == "submit":
                    data["chars"] = len(msg["args"]["text"])
                    await self.state("generating")
                if msg["command"] == "read_last_reply":
                    data["text"] = f"report from {self.profile_id}"
                if msg["command"] == "selector_test":
                    data = {"composer": True, "send": True, "stop": False, "keep_working": False, "human_check": False}
                await self.ws.send(json.dumps({"type": "result", "request_id": msg["request_id"], "ok": True, "data": data}))
        except websockets.ConnectionClosed:
            pass

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
        if self.ws:
            await self.ws.close()


async def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


# --------------------------------------------------------------------------- no restrictions remain


def test_removed_restrictions_are_gone_from_code_config_and_env():
    removed = ("ARENA_AUTOMATION_APPROVED", "ARENA_APPROVAL_REFERENCE", "ARENA_DOMAINS",
               "ARENA_MIN_REQUEST_INTERVAL_SECONDS", "ARENA_DAILY_CAP_PER_PROFILE")
    quota_words = re.compile(r"daily_cap|min_interval|requests_today|count_today|approval_reference|automation_approved")
    files = [ROOT / ".env.example", ROOT / "docker-compose.yml", ROOT / "scripts/ubuntu/start.sh"]
    for folder, pattern in ((ROOT / "backend/app", "*.py"), (ROOT / "bridge", "*.py"), (ROOT / "frontend/src", "*.ts*")):
        files.extend(p for p in folder.rglob(pattern) if "node_modules" not in p.parts and "__pycache__" not in p.parts)
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(name in text for name in removed) or quota_words.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    env_example = (ROOT / ".env.example").read_text()
    for needed in ("ARENA_CONNECTOR_ENABLED=", "ARENA_ALLOWED_ORIGINS=", "ARENA_AGENT_NEW_SESSION_URL=", "ARENA_AGENT_MAX_KEEP_WORKING=0"):
        assert needed in env_example


# --------------------------------------------------------------------------- bridge: many independent sessions


async def test_bridge_drives_many_independent_agent_profiles_with_validated_commands(bridge):
    profiles = [f"agent-p{i}" for i in range(7)]  # more than the old four-profile default
    pages: list[FakeAgentPage] = []
    async with httpx.AsyncClient(base_url=bridge.base, headers=ADMIN, timeout=20) as http:
        for pid in profiles:
            r = await http.put(f"/v1/connector/profiles/{pid}", json=_reg(f"tok-{pid}", bridge.base,
                                                                         new_session_url=f"{bridge.base}/mock-arena/?new=1"))
            assert r.status_code == 200 and r.json()["mode"] == "agent", r.text
            page = FakeAgentPage(bridge, pid, f"tok-{pid}")
            config = await page.connect()
            assert config["mode"] == "agent" and set(config["commands"]) == {
                "new_session", "submit", "keep_working", "read_last_reply", "selector_test", "select_repo"}
            pages.append(page)

        async def submit(pid: str, text: str):
            return await http.post(f"/v1/connector/profiles/{pid}/command", json={"command": "submit", "args": {"text": text}})

        # Every profile works at the same time; each page only sees its own prompt.
        results = await asyncio.gather(*(submit(pid, f"task for {pid}") for pid in profiles))
        assert all(r.status_code == 200 for r in results), [r.text for r in results]
        for page in pages:
            assert [c["args"]["text"] for c in page.commands] == [f"task for {page.profile_id}"]
        await _wait(lambda: all(bridge.hub.profiles[p].agent_state == "generating" for p in profiles))

        # Independent state: one profile goes idle, the others keep generating.
        await pages[0].state("idle")
        await _wait(lambda: bridge.hub.profiles[profiles[0]].agent_state == "idle")
        assert {bridge.hub.profiles[p].agent_state for p in profiles[1:]} == {"generating"}
        detail = (await http.get(f"/v1/connector/profiles/{profiles[0]}")).json()
        assert detail["agent_state"] == "idle" and any(e["kind"] == "state:idle" for e in detail["recent_events"])

        # Validation happens before anything reaches the page.
        bad = [
            ({"command": "rm -rf"}, 422, "unknown_command"),
            ({"command": "submit", "args": {"text": "  "}}, 422, "text_required"),
            ({"command": "submit", "args": {"text": "x" * 120_001}}, 413, "prompt_too_large"),
            ({"command": "new_session", "args": {"url": "https://evil.example/new"}}, 422, "invalid_new_session_url"),
        ]
        sent_before = len(pages[1].commands)
        for body, status, code in bad:
            r = await http.post(f"/v1/connector/profiles/{profiles[1]}/command", json=body)
            assert r.status_code == status and r.json()["detail"]["code"] == code, (body, r.text)
        assert len(pages[1].commands) == sent_before
        # new_session navigates only to the registered URL.
        r = await http.post(f"/v1/connector/profiles/{profiles[1]}/command", json={"command": "new_session"})
        assert r.status_code == 200 and pages[1].commands[-1]["args"]["url"] == f"{bridge.base}/mock-arena/?new=1"
        # A registration whose new-session URL leaves the allowed origins is refused.
        r = await http.put("/v1/connector/profiles/x", json=_reg("t", bridge.base, new_session_url="https://evil.example/"))
        assert r.status_code == 422

        # needs_human stops only that profile; read-only commands still work; reset resumes.
        await pages[2].state("needs_human", "human check element on page")
        await _wait(lambda: bridge.hub.profiles[profiles[2]].status == "needs_human")
        blocked = await submit(profiles[2], "more work")
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "needs_human"
        probe = await http.post(f"/v1/connector/profiles/{profiles[2]}/command", json={"command": "selector_test"})
        assert probe.status_code == 200 and probe.json()["result"]["composer"] is True
        assert (await submit(profiles[3], "unaffected")).status_code == 200
        reset = (await http.post(f"/v1/connector/profiles/{profiles[2]}/reset")).json()
        assert reset["status"] == "ready" and reset["agent_state"] == "ready"
        assert (await submit(profiles[2], "after resume")).status_code == 200

        # No quota: many commands in a row on one profile, no delays.
        for i in range(12):
            assert (await http.post(f"/v1/connector/profiles/{profiles[4]}/command", json={"command": "read_last_reply"})).status_code == 200
        assert bridge.hub.profiles[profiles[4]].commands_total == 13
    for page in pages:
        await page.close()


async def test_disconnect_fails_inflight_command_offline_and_reconnect_recovers(bridge):
    pid = "agent-recover"
    async with httpx.AsyncClient(base_url=bridge.base, headers=ADMIN, timeout=20) as http:
        assert (await http.put(f"/v1/connector/profiles/{pid}", json=_reg("tok-r", bridge.base))).status_code == 200
        page = FakeAgentPage(bridge, pid, "tok-r")
        await page.connect()
        await page.state("generating")
        pending = asyncio.create_task(http.post(f"/v1/connector/profiles/{pid}/command",
                                                json={"command": "submit", "args": {"text": "HANG please"}}))
        await _wait(lambda: len(page.commands) == 1)
        await page.close()  # browser crashed / tab closed
        r = await pending
        assert r.status_code == 503 and r.json()["detail"]["code"] == "offline"
        state = (await http.get(f"/v1/connector/profiles/{pid}")).json()
        assert state["agent_state"] == "offline" and state["connected"] is False
        offline = await http.post(f"/v1/connector/profiles/{pid}/command", json={"command": "selector_test"})
        assert offline.status_code == 503

        # The page reloads and the userscript reconnects: monitoring resumes with its history.
        again = FakeAgentPage(bridge, pid, "tok-r")
        await again.connect()
        await again.state("idle")
        await _wait(lambda: bridge.hub.profiles[pid].agent_state == "idle")
        state = (await http.get(f"/v1/connector/profiles/{pid}")).json()
        kinds = [e["kind"] for e in state["recent_events"]]
        assert state["connected"] and state["connections_total"] == 2
        assert "disconnected" in kinds and kinds.count("connected") == 2
        assert (await http.post(f"/v1/connector/profiles/{pid}/command", json={"command": "read_last_reply"})).status_code == 200
        # Re-registration (token rotation / backend restart) keeps the session history.
        assert (await http.put(f"/v1/connector/profiles/{pid}", json=_reg("tok-r", bridge.base))).json()["connections_total"] == 2
        await again.close()


# --------------------------------------------------------------------------- backend: mode, autoscaler, ownership


async def test_every_worker_can_switch_to_agent_mode_and_registration_has_no_limits(env, arena_settings, monkeypatch, bridge):
    factory = env
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", f"{bridge.base}/mock-arena/")
    monkeypatch.setattr(arena_settings, "ARENA_BRIDGE_URL", bridge.base)
    monkeypatch.setattr(arena_settings, "ARENA_AGENT_NEW_SESSION_URL", "")
    headers = await _admin_headers(factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/arena/profiles/profile-backend-docs/userscript", headers=headers)
        assert r.status_code == 200
        r = await client.put("/api/v1/arena/agents/mode", json={"mode": "agent"}, headers=headers)
        assert r.status_code == 200, r.text
        async with factory() as db:
            workers = [w for w in (await db.execute(select(Agent).where(Agent.role == "worker", Agent.is_enabled.is_(True)))).scalars()
                       if w.browser_profile_id]
        assert r.json()["count"] == len(workers) >= 4
        registered = {c["profile_id"]: c["bridge_registration"] for c in r.json()["changed"]}
        assert registered["profile-backend-docs"]["ok"] is True and registered["profile-backend-docs"]["mode"] == "agent"
        assert all(reg.get("skipped") for pid, reg in registered.items() if pid != "profile-backend-docs")
        live = bridge.hub.profiles["profile-backend-docs"]
        assert live.mode == "agent"
        overview = (await client.get("/api/v1/arena/agent/overview", headers=headers)).json()
        # R7: plus the manager's own agent-mode profile.
        assert overview["counts"]["agent_mode_profiles"] == len(workers) + 1 and overview["manager"]["arena_mode"] == "agent"
        assert {row["arena_mode"] for row in overview["employees"]} == {"agent"}
        wall = (await client.get("/api/v1/arena/profiles", headers=headers)).json()
        assert {c["arena_mode"] for c in wall["profiles"] if c["agent_role"] == "worker"} == {"agent"}
        bad = await client.put("/api/v1/arena/agents/worker-1/mode", json={"mode": "turbo"}, headers=headers)
        assert bad.status_code == 422
    async with factory() as db:
        assert await is_arena_agent_worker(db, await db.get(Agent, "worker-1")) is True
        manager = await db.get(Agent, "manager-0")
        assert await is_arena_agent_worker(db, manager) is False


async def test_autoscaler_assigns_agent_mode_without_a_profile_ceiling(env, arena_settings, monkeypatch):
    factory = env
    monkeypatch.setattr(arena_settings, "MAX_CONCURRENT_WORKERS", 0)  # no ceiling
    async with factory() as db:
        template = await db.get(BrowserProfile, (await db.get(Agent, "worker-1")).browser_profile_id)
        template.arena_mode = "agent"
        await db.commit()
        before = len((await db.execute(select(Agent).where(Agent.role == "worker"))).scalars().all())
        created = await autoscale_service.add_workers(db, 30, actor="test", reason="backlog")
        assert len(created) == 30  # more than the 25 default ceiling
        for agent in created:
            profile = await db.get(BrowserProfile, agent.browser_profile_id)
            assert profile is not None and profile.arena_mode == "agent"
        assert len((await db.execute(select(Agent).where(Agent.role == "worker"))).scalars().all()) == before + 30
        status = await autoscale_service.status(db)
        assert status["max_workers"] is None
    # A ceiling, when the owner sets one, is a host-capacity choice and is still honoured.
    monkeypatch.setattr(arena_settings, "MAX_CONCURRENT_WORKERS", before + 31)
    async with factory() as db:
        assert len(await autoscale_service.add_workers(db, 5, actor="test", reason="backlog")) == 1


async def test_session_ownership_reuse_conflict_and_new_session_after_merge(env):
    factory = env
    async with factory() as db:
        project = Project(name="R1", slug="r1", repo_path=".", default_branch="main")
        db.add(project)
        await db.flush()
        t1, t2 = (Task(project_id=project.id, title=f"t{i}", status="in_progress") for i in (1, 2))
        db.add_all([t1, t2])
        await db.flush()
        w1, w2 = await db.get(Agent, "worker-1"), await db.get(Agent, "worker-2")

        s1, fresh = await claim_session(db, w1, t1)
        assert fresh is True and s1.agent_id == "worker-1" and s1.profile_id == w1.browser_profile_id
        s2, _ = await claim_session(db, w2, t2)
        assert s2.profile_id != s1.profile_id  # independent per employee

        again, fresh = await claim_session(db, w1, t1)  # recovery after a restart reuses it
        assert again.id == s1.id and fresh is True
        s1.submitted_at = s1.created_at
        assert (await claim_session(db, w1, t1))[1] is False

        with pytest.raises(ArenaSessionConflict):  # one tab drives one task at a time
            await claim_session(db, w1, t2)

        t1.status = "done"
        await close_session(db, s1, phase="merged", reason="PR merged")
        t3 = Task(project_id=project.id, title="t3", status="in_progress")
        db.add(t3)
        await db.flush()
        s3, fresh = await claim_session(db, w1, t3)
        assert s3.id != s1.id and fresh is True and s1.is_open is False
        await db.commit()
        open_rows = (await db.execute(select(ArenaSession).where(ArenaSession.is_open.is_(True)))).scalars().all()
        assert {r.profile_id for r in open_rows} == {w1.browser_profile_id, w2.browser_profile_id}

        # Bridge state mirroring: changes recorded, needs_human reason kept, offline when disconnected.
        assert apply_bridge_state(s3, {"connected": True, "agent_state": "generating", "agent_state_since": "2026-11-16T10:00:00+00:00"})
        assert s3.page_state == "generating" and s3.last_state_change_at.year == 2026
        assert not apply_bridge_state(s3, {"connected": True, "agent_state": "generating"})
        apply_bridge_state(s3, {"connected": True, "status": "needs_human", "needs_human_reason": "captcha"})
        assert s3.page_state == "needs_human" and s3.needs_human_reason == "captcha"
        apply_bridge_state(s3, {"connected": False})
        assert s3.page_state == "offline"
