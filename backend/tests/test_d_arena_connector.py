"""Item D / Phase 6 — Profiles Manager + Arena browser connector.

Acceptance (docs/AATM_BUILD_PLAN.md §Phase 6):
* flags off -> every real-Arena path returns ``not_enabled``;
* against the local mock Arena page the 4 default worker profiles process 4 tasks in parallel
  through userscript -> bridge -> AgentLoop -> worktree -> draft PR;
* a simulated human-check element stops that profile with ``needs_human``;
* planted fake tokens never appear in logs (or the database).

The browser side is real headless Chromium (Playwright) loading the mock page served by the real
bridge; the generated userscript is added to the page by the *test* (standing in for the human
installing it in Tampermonkey). AATM itself never injects scripts (run_userscript stays 501).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import websockets
from bridge.server import create_app as create_bridge_app
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_b_review_merge_completion import REPO_SLUG, _init_repo, fake_gh  # noqa: F401 - fixture re-export

from app.browser.arena_connector import arena_gate, build_userscript, match_patterns
from app.browser.engines import validate_userscript_policy
from app.browser.manager import browser_manager, detect_playwright_capabilities
from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import Agent, AIProvider, BrowserProfile, Notification, Project, Task, User, Userscript
from app.orchestration.scheduler import TaskScheduler
from app.providers.adapters import ArenaBrowserAdapter
from app.providers.base import ProviderRequest
from app.runtime.agent_loop import AgentLoop

BRIDGE_TOKEN = "bridge-test-token-phase6-0123456789"
PLANTED = ("MOCK-PLANTED-CSRF-9d41c2", "MOCK-PLANTED-COOKIE-5be7aa", "MOCK-PLANTED-STORAGE-1c08ef")
ARENA_FLAGS = ("ARENA_CONNECTOR_ENABLED", "ARENA_AUTOMATION_APPROVED", "ARENA_APPROVAL_REFERENCE")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BridgeThread:
    """The real bridge app (connector hub + mock Arena page) on a loopback port."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.app = create_bridge_app()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", ws="websockets"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def hub(self):
        return self.app.state.connector_hub

    def __enter__(self) -> BridgeThread:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("ARENA_REPLY_TIMEOUT_SECONDS", "20")
    for flag in ARENA_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    with BridgeThread() as b:
        yield b


@pytest.fixture()
def arena_settings(monkeypatch):
    settings = get_settings()
    for name in ARENA_FLAGS:
        monkeypatch.setattr(settings, name, "" if name == "ARENA_APPROVAL_REFERENCE" else False)
    monkeypatch.setattr(settings, "ARENA_MOCK_URL", "")
    monkeypatch.setattr(settings, "ARENA_MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    return settings


@pytest.fixture()
async def env(tmp_path: Path, monkeypatch, arena_settings):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase6.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arena_settings, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(arena_settings, "BROWSER_PROFILES_DIR", tmp_path / "profiles")
    async with factory() as db:
        await seed_initial_data(db)
        await db.execute(update(Task).values(status="completed"))
        await db.execute(update(Agent).values(status="idle", current_task_id=None))
        await db.commit()

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    yield factory
    app.dependency_overrides.pop(get_db, None)
    await engine.dispose()


async def _admin_headers(factory) -> dict[str, str]:
    async with factory() as db:
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        admin.must_change_password = False
        await db.commit()
        return {"Authorization": f"Bearer {create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)}"}


def _replies_param(replies: list[dict[str, Any]]) -> str:
    raw = json.dumps([json.dumps(r) for r in replies]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- flags off


async def test_flags_off_every_real_arena_path_returns_not_enabled(env, arena_settings, monkeypatch):
    factory = env
    gate = arena_gate()
    assert gate["mode"] == "disabled" and gate["code"] == "not_enabled" and gate["real_enabled"] is False

    # Partial approvals never enable real Arena.
    for connector, approved, ref in ((True, False, "REF-1"), (True, True, ""), (False, True, "REF-1")):
        monkeypatch.setattr(arena_settings, "ARENA_CONNECTOR_ENABLED", connector)
        monkeypatch.setattr(arena_settings, "ARENA_AUTOMATION_APPROVED", approved)
        monkeypatch.setattr(arena_settings, "ARENA_APPROVAL_REFERENCE", ref)
        assert arena_gate()["real_enabled"] is False and arena_gate()["mode"] == "disabled"
    for name in ARENA_FLAGS:
        monkeypatch.setattr(arena_settings, name, "" if name == "ARENA_APPROVAL_REFERENCE" else False)
    # A "mock" URL that is not loopback is refused (it would be a back door to the real site).
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", "https://arena.ai/")
    assert arena_gate()["mode"] == "disabled" and arena_gate()["mock_url_rejected"] is True
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", "")

    adapter = ArenaBrowserAdapter("arena-browser", "http://127.0.0.1:4100", "env:ARENA_BRIDGE_TOKEN", "arena_browser")
    res = await adapter.generate(ProviderRequest(prompt="hi", metadata={"browser_profile_id": "profile-qa-e2e"}))
    assert res.success is False and res.metadata["code"] == "not_enabled" and res.content == ""
    assert (await adapter.health_check()).detail.startswith("not_enabled")

    headers = await _admin_headers(factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = (await client.get("/api/v1/arena/connector/status", headers=headers)).json()
        assert status["status_label"] == "waiting_for_arena_approval" and status["enabled"] is False
        for path in ("userscript", "register", "open-for-sign-in"):
            r = await client.post(f"/api/v1/arena/profiles/profile-qa-e2e/{path}", headers=headers)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "not_enabled", (path, r.text)
        wall = (await client.get("/api/v1/arena/profiles", headers=headers)).json()
        assert len([c for c in wall["profiles"] if c["agent_role"] == "worker"]) == 4
        assert len({c["color"] for c in wall["profiles"]}) == len(wall["profiles"])
        assert {c["connector_status"] for c in wall["profiles"]} == {"offline"}

    # Workers keep their configured provider; the arena-browser provider is not even created.
    async with factory() as db:
        worker = await db.get(Agent, "worker-1")
        assert await AgentLoop._preferred_provider_id(db, worker) == worker.provider_id
        assert await db.get(AIProvider, "arena-browser") is None
        profile = await db.get(BrowserProfile, "profile-qa-e2e")
        # AATM never injects scripts.
        with pytest.raises(HTTPException) as exc:
            await browser_manager.run_userscript(db, profile, None, "worker-4", "worker")
        assert exc.value.status_code == 501


def test_bridge_refuses_real_arena_origins_unless_the_bridge_itself_is_approved(bridge, monkeypatch):
    import httpx

    headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
    body = {"token_sha256": "a" * 64, "agent_id": "worker-1", "allowed_origins": ["https://arena.ai"], "real_enabled": False}
    r = httpx.put(f"{bridge.base}/v1/connector/profiles/p1", json=body, headers=headers)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_enabled"
    r = httpx.put(f"{bridge.base}/v1/connector/profiles/p1", json={**body, "real_enabled": True}, headers=headers)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_enabled"  # bridge env flags are off
    monkeypatch.setenv("ARENA_CONNECTOR_ENABLED", "true")
    monkeypatch.setenv("ARENA_AUTOMATION_APPROVED", "true")
    monkeypatch.setenv("ARENA_APPROVAL_REFERENCE", "OWNER-APPROVAL-REF")
    r = httpx.put(f"{bridge.base}/v1/connector/profiles/p1", json={**body, "real_enabled": True}, headers=headers)
    assert r.status_code == 200
    # Admin endpoints always need the bridge token.
    assert httpx.get(f"{bridge.base}/v1/connector/profiles").status_code == 401
    # Unknown profile through the completion endpoint -> not_registered, never a fake answer.
    r = httpx.post(f"{bridge.base}/v1/chat/completions", headers={**headers, "X-AATM-Profile-Id": "nope"},
                   json={"model": "arena_browser", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_registered"


# --------------------------------------------------------------------------- userscript


async def test_generated_userscript_is_scoped_minimal_and_token_is_stored_hashed_only(env, arena_settings, monkeypatch, bridge):
    factory = env
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", f"{bridge.base}/mock-arena/")
    monkeypatch.setattr(arena_settings, "ARENA_BRIDGE_URL", bridge.base)
    headers = await _admin_headers(factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/arena/profiles/profile-backend-docs/userscript", headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
    script = data["script"]
    assert data["mode"] == "mock" and data["bridge_registration"]["ok"] is True
    assert data["match_patterns"] == [f"{bridge.base}/mock-arena/*"]
    assert "// @grant        none" in script and "@grant GM" not in script
    assert "arena.ai" not in script.split("==/UserScript==")[0]  # real domain only after approval
    lowered = script.lower()
    for forbidden in ("cookie", "localstorage", "sessionstorage", "gm_xmlhttprequest", "unsafewindow", "eval(", "captcha"):
        assert forbidden not in lowered, forbidden
    assert validate_userscript_policy(script, data["match_patterns"]) == (True, "approved")
    token = json.loads(script.split("var CFG = ")[1].split(";\n")[0])["token"]
    async with factory() as db:
        profile = await db.get(BrowserProfile, "profile-backend-docs")
        assert profile.connector_token_hash == hashlib.sha256(token.encode()).hexdigest()
        stored = (await db.execute(select(Userscript).where(Userscript.name == "aatm-arena-connector-profile-backend-docs"))).scalar_one()
        assert token not in stored.code and stored.is_enabled is False
        dump = json.dumps([list(row) for row in (await db.execute(text("SELECT * FROM audit_logs"))).all()], default=str)
        assert token not in dump

    # With the owner's approval recorded, the real domain is added to @match.
    monkeypatch.setattr(arena_settings, "ARENA_CONNECTOR_ENABLED", True)
    monkeypatch.setattr(arena_settings, "ARENA_AUTOMATION_APPROVED", True)
    monkeypatch.setattr(arena_settings, "ARENA_APPROVAL_REFERENCE", "OWNER-REF-1")
    assert "https://arena.ai/*" in match_patterns()
    real_script = build_userscript(profile, None, "t")
    assert validate_userscript_policy(real_script, match_patterns()) == (True, "approved")


# --------------------------------------------------------------------------- bridge rules


async def test_bridge_connector_auth_origin_one_in_flight_delay_cap_and_needs_human(bridge):
    import httpx

    token = "profile-token-abc"
    origin = bridge.base
    admin = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
    reg = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "agent_id": "worker-1",
           "allowed_origins": [origin], "min_interval_seconds": 0.3, "daily_cap": 3}
    async with httpx.AsyncClient(base_url=bridge.base, headers=admin) as http:
        assert (await http.put("/v1/connector/profiles/p1", json=reg)).status_code == 200
        ws_url = f"ws://127.0.0.1:{bridge.port}/v1/connector"

        async def hello(tok: str, page_origin: str, header_origin: str) -> int | None:
            async with websockets.connect(ws_url, origin=header_origin) as ws:
                await ws.send(json.dumps({"type": "hello", "profile_id": "p1", "token": tok, "page_origin": page_origin}))
                try:
                    await asyncio.wait_for(ws.recv(), timeout=3)
                except websockets.ConnectionClosed as closed:
                    return closed.rcvd.code if closed.rcvd else None
                return None

        assert await hello("wrong", origin, origin) == 4401
        assert await hello(token, "http://127.0.0.1:9", "http://127.0.0.1:9") == 4401  # origin not allowed
        assert await hello(token, origin, "http://evil.example") == 4401  # header/page origin mismatch

        active = 0
        peak = 0
        prompts: list[float] = []

        async with websockets.connect(ws_url, origin=origin) as ws:
            await ws.send(json.dumps({"type": "hello", "profile_id": "p1", "token": token, "page_origin": origin}))
            config = json.loads(await ws.recv())
            assert config["type"] == "config" and config["stop_selectors"]

            async def fake_page():
                nonlocal active, peak
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg["type"] != "prompt":
                        continue
                    active += 1
                    peak = max(peak, active)
                    prompts.append(time.monotonic())
                    await asyncio.sleep(0.1)
                    active -= 1
                    if "STOP" in msg["prompt"]:
                        await ws.send(json.dumps({"type": "needs_human", "reason": "human check element on page"}))
                    else:
                        await ws.send(json.dumps({"type": "answer", "request_id": msg["request_id"], "text": "ok:" + msg["prompt"]}))

            page = asyncio.create_task(fake_page())

            async def ask(p: str):
                return await http.post("/v1/chat/completions", headers={"X-AATM-Profile-Id": "p1"},
                                       json={"model": "arena_browser", "messages": [{"role": "user", "content": p}]}, timeout=20)

            first, second = await asyncio.gather(ask("one"), ask("two"))
            assert {first.json()["choices"][0]["message"]["content"], second.json()["choices"][0]["message"]["content"]} == {"ok:one", "ok:two"}
            assert peak == 1, "one in-flight request per profile"
            assert prompts[1] - prompts[0] >= 0.3 + 0.1 - 0.02, "minimum delay between requests"

            stopped = await ask("please STOP here")
            assert stopped.status_code == 409 and stopped.json()["detail"]["code"] == "needs_human"
            again = await ask("anything")
            assert again.status_code == 409, "stays stopped until a human resets it"
            listed = (await http.get("/v1/connector/profiles")).json()["profiles"][0]
            assert listed["status"] == "needs_human" and listed["health"] == "red"
            sessions = (await http.get("/v1/sessions")).json()
            assert sessions["connector"]["profiles"][0]["status"] == "needs_human"

            assert (await http.post("/v1/connector/profiles/p1/reset")).json()["status"] == "ready"
            capped = await ask("four")
            assert capped.status_code == 429 and capped.json()["detail"]["code"] == "daily_cap_reached"
            # Re-registering (e.g. token rotation) never resets the daily counter.
            assert (await http.put("/v1/connector/profiles/p1", json=reg)).json()["requests_today"] == 3
            page.cancel()


# --------------------------------------------------------------------------- end to end (browser)

needs_chromium = pytest.mark.skipif(
    not detect_playwright_capabilities()["chromium_installed"], reason="Playwright Chromium not installed (playwright install chromium)"
)

WORKER_REPLIES = [
    {"action": "write", "path": "feature.py", "content": "def answer():\n    return 42\n"},
    {"action": "write", "path": "test_feature.py", "content": "from feature import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"},
    {"action": "done", "summary": "Implemented answer() with a test.", "deliverables": ["feature.py", "test_feature.py"]},
]


async def _setup_connector(factory, bridge, arena_settings, monkeypatch, tmp_path: Path, n_tasks: int):
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", f"{bridge.base}/mock-arena/")
    monkeypatch.setattr(arena_settings, "ARENA_BRIDGE_URL", bridge.base)
    repo, origin = tmp_path / "demo", tmp_path / "origin.git"
    _init_repo(repo, origin)
    async with factory() as db:
        project = Project(name="Phase6", slug="phase6", repo_path=str(repo), repo_url=f"https://github.com/{REPO_SLUG}",
                          default_branch="main", config={"worktree_mode": True, "auto_pr": True})
        db.add(project)
        await db.flush()
        tasks = [Task(project_id=project.id, title=f"Connector task {i}", description="Add answer().", status="queued",
                      execution_mode="automatic", division_code="D02") for i in range(n_tasks)]
        db.add_all(tasks)
        # These are coding tasks: the admin gives every default worker file-edit permission
        # (seeded QA/provider specialists cannot edit files by design).
        for w in (await db.execute(select(Agent).where(Agent.role == "worker"))).scalars():
            w.permissions = sorted(set(w.permissions or []) | {"edit_backend"})
        await db.commit()
        task_ids = [t.id for t in tasks]
    headers = await _admin_headers(factory)
    scripts: dict[str, str] = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with factory() as db:
            workers = list((await db.execute(select(Agent).where(Agent.role == "worker").order_by(Agent.id))).scalars())
        for w in workers:
            r = await client.post(f"/api/v1/arena/profiles/{w.browser_profile_id}/userscript", headers=headers)
            assert r.status_code == 200 and r.json()["bridge_registration"]["ok"], r.text
            scripts[w.id] = r.json()["script"]
    return task_ids, workers, scripts, headers


async def _open_pages(playwright, workers, scripts, bridge, page_params: dict[str, str]):
    # Same executable resolution as BrowserManager (the cached Chromium build on this host).
    executable = detect_playwright_capabilities().get("executable_path")
    browser = await playwright.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
    pages = []
    for w in workers:
        context = await browser.new_context()  # separate profile per employee
        page = await context.new_page()
        await page.goto(f"{bridge.base}/mock-arena/?{page_params[w.id]}")
        # Stands in for the human installing the reviewed userscript in Tampermonkey.
        await page.add_script_tag(content=scripts[w.id])
        pages.append(page)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready = [p for p in bridge.hub.summaries() if p["status"] == "ready"]
        if len(ready) >= len(workers):
            break
        await asyncio.sleep(0.05)
    return browser, pages


@needs_chromium
async def test_four_default_profiles_process_four_tasks_in_parallel_to_draft_prs(env, bridge, arena_settings, monkeypatch, tmp_path, fake_gh, caplog):  # noqa: F811
    from playwright.async_api import async_playwright

    caplog.set_level(logging.DEBUG)
    factory = env
    task_ids, workers, scripts, _ = await _setup_connector(factory, bridge, arena_settings, monkeypatch, tmp_path, 4)
    assert arena_gate()["mode"] == "mock"
    params = {w.id: f"delay_ms=700&replies={_replies_param(WORKER_REPLIES)}" for w in workers}

    calls: list[dict[str, Any]] = []
    original = ArenaBrowserAdapter.generate

    async def recording(self, req):
        start = time.monotonic()
        res = await original(self, req)
        calls.append({"agent": req.agent_id, "start": start, "end": time.monotonic(), "ok": res.success, "code": res.metadata.get("code")})
        return res

    monkeypatch.setattr(ArenaBrowserAdapter, "generate", recording)

    async with async_playwright() as pw:
        browser, _pages = await _open_pages(pw, workers, scripts, bridge, params)
        try:
            assert len([p for p in bridge.hub.summaries() if p["status"] == "ready"]) == 4
            async with factory() as db:
                cycle = await TaskScheduler().run_orchestration_cycle(db)
            assert len(cycle["dispatched"]) == 4 and len({d["worker_id"] for d in cycle["dispatched"]}) == 4
            loop = AgentLoop(session_factory=factory, poll_seconds=0.05)
            runner = asyncio.create_task(loop.run_forever())
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    async with factory() as db:
                        tasks = [await db.get(Task, tid) for tid in task_ids]
                        for t in tasks:
                            await db.refresh(t)
                    if all(t.pr_number for t in tasks) or any(t.status in ("failed", "blocked") for t in tasks):
                        break
                    await asyncio.sleep(0.2)
            finally:
                await loop.stop()
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
        finally:
            await browser.close()

    if [t.status for t in tasks] != ["pr_open"] * 4:  # diagnostics
        async with factory() as db:
            from app.models import AgentMessage
            msgs = list((await db.execute(select(AgentMessage))).scalars())
            detail = [(m.recipient_id, m.execution_status, json.dumps(m.response_payload, default=str)[:1500]) for m in msgs if m.task_id in task_ids]
        pytest.fail(json.dumps({"tasks": [(t.assigned_agent_id, t.status, t.error_message) for t in tasks], "msgs": detail, "calls": calls}, default=str, indent=1)[:12000])
    assert len({t.branch_name for t in tasks}) == 4 and len({t.worktree_path for t in tasks}) == 4
    pr_creates = [c for c in fake_gh if c[1:3] == ["pr", "create"]]
    assert len(pr_creates) == 4 and all("--draft" in c for c in pr_creates)
    assert all(c["ok"] for c in calls) and len(calls) == 12
    firsts = [min((c for c in calls if c["agent"] == w.id), key=lambda c: c["start"]) for w in workers]
    assert max(c["start"] for c in firsts) < min(c["end"] for c in firsts), "the 4 profiles answered in parallel"
    summaries = {p["profile_id"]: p for p in bridge.hub.summaries()}
    assert all(summaries[w.browser_profile_id]["requests_total"] == 3 for w in workers)
    async with factory() as db:
        used = {(await db.get(Agent, w.id)).id: w.browser_profile_id for w in workers}
        assert len(set(used.values())) == 4  # no account pooling: each employee used its own profile
        await _assert_no_planted_tokens(db, caplog.text)


async def _assert_no_planted_tokens(db, log_text: str) -> None:
    dumps = [log_text]
    for table in ("audit_logs", "provider_usage_logs", "agent_messages", "chat_messages", "tasks", "notifications", "userscripts"):
        rows = (await db.execute(text(f"SELECT * FROM {table}"))).all()
        dumps.append(json.dumps([list(r) for r in rows], default=str))
    blob = "\n".join(dumps)
    for token in PLANTED:
        assert token not in blob, f"planted token leaked: {token}"


@needs_chromium
async def test_human_check_stops_that_profile_with_needs_human_and_resume_requeues(env, bridge, arena_settings, monkeypatch, tmp_path, fake_gh, caplog):  # noqa: F811
    from playwright.async_api import async_playwright

    caplog.set_level(logging.DEBUG)
    factory = env
    task_ids, workers, scripts, headers = await _setup_connector(factory, bridge, arena_settings, monkeypatch, tmp_path, 1)
    target = workers[0]
    params = {w.id: f"delay_ms=300&human_check_after=0&replies={_replies_param(WORKER_REPLIES)}" for w in workers}

    async with async_playwright() as pw:
        browser, _pages = await _open_pages(pw, [target], scripts, bridge, params)
        try:
            async with factory() as db:
                # Pin the single task to the target worker.
                task = await db.get(Task, task_ids[0])
                task.assigned_agent_id = target.id
                task.execution_mode = "manual"
                await db.commit()
                cycle = await TaskScheduler().run_orchestration_cycle(db)
            assert cycle["dispatched"][0]["worker_id"] == target.id
            loop = AgentLoop(session_factory=factory, poll_seconds=0.05)
            runner = asyncio.create_task(loop.run_forever())
            try:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    async with factory() as db:
                        task = await db.get(Task, task_ids[0])
                    if task.status in ("blocked", "failed", "pr_open"):
                        break
                    await asyncio.sleep(0.1)
            finally:
                await loop.stop()
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
        finally:
            await browser.close()

    assert task.status == "blocked", (task.status, task.error_message)
    assert task.checkpoints[-1]["event"] == "needs_human"
    summary = {p["profile_id"]: p for p in bridge.hub.summaries()}[target.browser_profile_id]
    assert summary["status"] == "needs_human" and summary["health"] == "red"
    async with factory() as db:
        profile = await db.get(BrowserProfile, target.browser_profile_id)
        agent = await db.get(Agent, target.id)
        assert profile.connector_status == "needs_human" and agent.status == "needs_human"
        notes = list((await db.execute(select(Notification).where(Notification.category == "arena"))).scalars())
        assert notes and notes[-1].severity == "critical"
        # The router did NOT fail over to a paid API provider.
        assert task.retry_count == 0
        await _assert_no_planted_tokens(db, caplog.text)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/api/v1/arena/profiles/{target.browser_profile_id}/resume", headers=headers)
        assert r.status_code == 200 and r.json()["requeued_tasks"] == task_ids
    async with factory() as db:
        assert (await db.get(Task, task_ids[0])).status == "queued"
        assert (await db.get(Agent, target.id)).status == "idle"


async def test_owner_configured_dom_selectors_reach_the_bridge_as_data(env, arena_settings, monkeypatch, bridge):
    factory = env
    monkeypatch.setattr(arena_settings, "ARENA_MOCK_URL", f"{bridge.base}/mock-arena/")
    monkeypatch.setattr(arena_settings, "ARENA_BRIDGE_URL", bridge.base)
    monkeypatch.setattr(arena_settings, "ARENA_DOM_SELECTORS", json.dumps({"input": "#composer", "evil": "x"}))
    headers = await _admin_headers(factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/arena/profiles/profile-backend-docs/userscript", headers=headers)
        assert r.status_code == 200 and r.json()["bridge_registration"]["ok"], r.text
    state = bridge.hub.profiles["profile-backend-docs"]
    assert state.dom["input"] == "#composer" and "evil" not in state.dom
    monkeypatch.setattr(arena_settings, "ARENA_DOM_SELECTORS", "not json")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/arena/profiles/profile-backend-docs/register", headers=headers)
        assert r.json()["code"] == "invalid_dom_selectors"
