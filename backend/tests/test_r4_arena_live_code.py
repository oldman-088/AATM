"""R4 — live code from Arena Agent branches over the Command Bridge ``agent.code`` event.

The Arena Agent pushes to its own ``arena/*`` branch on GitHub; AATM reads the changes through the
GitHub compare API (here: the fake ``gh`` backed by a real bare repository) and streams them as
redacted, size-limited, throttled ``agent.code`` frames tagged with employee, task and branch.
"""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_d_arena_connector
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from test_b1_code_stream import captured  # noqa: F401 - fixture
from test_d_arena_connector import _admin_headers, arena_settings, env  # noqa: F401 - fixtures
from test_r3_arena_agent_workflow import ArenaOnGitHub, _runner_fixture

import app.main as main_module
from app.core.events import event_bus
from app.main import app
from app.models import ArenaSession
from app.runtime.code_stream import (
    MAX_FRAME_BYTES,
    build_github_frame,
    code_stream,
    parse_unified_patch,
)

PATCH = "@@ -1,3 +1,4 @@ def answer():\n-    return 41\n+    return 42\n+# done\n context line\n\\ No newline at end of file"
FAKE_KEY = "sk-" + "live0123456789abcdefghijklmnop"


def _frames(events: list[dict]) -> list[dict]:
    return [f for e in events if e["type"] == "agent.code" for f in e["payload"]["frames"]]


def test_unified_patch_becomes_add_del_context_lines():
    lines = parse_unified_patch(PATCH)
    assert lines[0]["type"] == "gap"
    assert {"type": "del", "text": "    return 41"} in lines and {"type": "add", "text": "    return 42"} in lines
    assert {"type": "ctx", "text": "context line"} in lines
    assert not any("No newline" in line["text"] for line in lines)
    big = "@@ -1 +1,500 @@\n" + "\n".join(f"+line {i}" for i in range(500))
    capped = parse_unified_patch(big, max_lines=50)
    assert len(capped) == 51 and capped[-1]["text"] == "… diff truncated …"


def test_github_frames_are_redacted_size_limited_and_skip_sensitive_files():
    frame = build_github_frame(agent_id="worker-1", task_id="t1", branch="arena/x", filename="app/config.py",
                               patch=f'@@ -0,0 +1 @@\n+API_TOKEN = "{FAKE_KEY}"\n+ok = 1', additions=2, head_sha="a" * 40,
                               compare_url="https://github.com/acme/demo/compare/main...arena/x")
    assert frame is not None and frame["source"] == "arena_github" and frame["action"] == "edit"
    assert frame["branch"] == "arena/x" and frame["agent_id"] == "worker-1" and frame["task_id"] == "t1"
    assert FAKE_KEY not in json.dumps(frame) and "[REDACTED]" in json.dumps(frame)
    for path in (".env", "deploy/secrets.yaml", "keys/server.pem", ".git/config"):
        assert build_github_frame(agent_id="w", task_id="t", branch="arena/x", filename=path, patch="@@ -0,0 +1 @@\n+x") is None
    huge = build_github_frame(agent_id="w", task_id="t", branch="arena/x", filename="big.py",
                              patch="@@ -0,0 +1,9000 @@\n" + "\n".join("+" + "x" * 60 for _ in range(9000)))
    assert huge["truncated"] is True and len(json.dumps(huge).encode()) < MAX_FRAME_BYTES * 3
    assert build_github_frame(agent_id="w", task_id="t", branch="arena/x", filename="k.py",
                              patch="@@ -0,0 +1 @@\n+-----BEGIN RSA PRIVATE KEY-----") is None


async def test_runner_streams_branch_changes_throttled_per_employee_task_and_branch(env, arena_settings, monkeypatch, tmp_path, captured):
    code_stream.clear()
    page, runner, gh, (task_id, session_id, agent_id), origin = await _runner_fixture(env, arena_settings, monkeypatch, tmp_path)
    runner.live_code_seconds = 0
    arena = ArenaOnGitHub(tmp_path, origin, gh)
    arena.push(task_id, "arena/live1", {"answer.py": "def answer():\n    return 42\n", ".env": f"KEY={FAKE_KEY}\n",
                                        "conf/token_store.py": "x = 1\n"}, "first")
    async with env() as db:
        await runner.tick(db)
    await code_stream.drain()
    frames = _frames(captured)
    assert [f["path"] for f in frames] == ["answer.py"], "sensitive files are never streamed"
    assert frames[0]["agent_id"] == agent_id and frames[0]["task_id"] == task_id and frames[0]["branch"] == "arena/live1"
    assert frames[0]["compare_url"].endswith("/compare/main...arena/live1") and frames[0]["additions"] == 2
    live = [e["payload"] for e in captured if e["type"] == "arena.live_diff"]
    assert live and live[-1]["branch"] == "arena/live1" and {f["filename"] for f in live[-1]["files"]} == {"answer.py"}
    assert live[-1]["hidden_files"] == 2 and live[-1]["commits"][0]["message"] == "first"

    # Same head again: no repeated update.
    captured.clear()
    async with env() as db:
        await runner.tick(db)
    await code_stream.drain()
    assert _frames(captured) == []

    # A new commit: only the file whose diff changed is streamed again.
    arena.push(task_id, "arena/live1", {"tests/test_answer.py": "def test_answer():\n    assert True\n"}, "second")
    async with env() as db:
        await runner.tick(db)
    await code_stream.drain()
    assert [f["path"] for f in _frames(captured)] == ["tests/test_answer.py"]

    # Throttle: a third push inside the interval is not published until the interval passed.
    runner.live_code_seconds = 3600
    captured.clear()
    arena.push(task_id, "arena/live1", {"answer.py": "def answer():\n    return 43\n"}, "third")
    async with env() as db:
        await runner.tick(db)
    await code_stream.drain()
    assert _frames(captured) == []

    headers = await _admin_headers(env)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get(f"/api/v1/arena/sessions/{session_id}/live-diff?refresh=true", headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ok"] and body["branch"] == "arena/live1" and body["ahead_by"] == 3
        assert {f["filename"] for f in body["files"]} == {"answer.py", "tests/test_answer.py"}
        assert [c["message"] for c in body["commits"]] == ["first", "second", "third"]
        assert FAKE_KEY not in res.text
        assert (await client.get("/api/v1/arena/sessions/missing/live-diff", headers=headers)).status_code == 404
    async with env() as db:
        assert (await db.get(ArenaSession, session_id)).pr_url.endswith("/pull/101")


async def test_github_frames_reach_authenticated_websocket_clients_as_agent_code(env, monkeypatch):
    from app.core.security import create_access_token
    from app.runtime.background_scheduler import BackgroundScheduler

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(BackgroundScheduler, "run_orchestration_forever", _noop)
    monkeypatch.setattr(BackgroundScheduler, "run_self_healing_forever", _noop)
    monkeypatch.setattr(main_module.settings, "AGENT_LOOP_ENABLED", False)
    monkeypatch.setattr(main_module, "AsyncSessionLocal", env)
    code_stream.clear()
    with TestClient(app) as sync_client:
        headers = await _admin_headers(env)  # after startup: the lifespan seed runs first
        token = headers["Authorization"].split(" ", 1)[1]
        assert create_access_token  # same token format as the REST API
        with sync_client.websocket_connect("/ws/events") as ws:
            ws.send_text(json.dumps({"token": token}))
            assert json.loads(ws.receive_text())["type"] == "auth.ok"
            frame = build_github_frame(agent_id="worker-2", task_id="task-ws", branch="arena/ws1", filename="ws.py",
                                       patch=f"@@ -0,0 +1 @@\n+secret_key = '{FAKE_KEY}'", additions=1)
            await code_stream.publish(frame)
            await code_stream.drain()
            message = json.loads(ws.receive_text())
            assert message["type"] == "agent.code"
            payload = message["payload"]
            assert payload["agent_id"] == "worker-2" and payload["task_id"] == "task-ws"
            assert payload["frames"][0]["branch"] == "arena/ws1" and payload["frames"][0]["source"] == "arena_github"
            assert FAKE_KEY not in json.dumps(message)
    assert not event_bus._connections
