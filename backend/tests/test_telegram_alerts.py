"""Telegram alerts against a mocked Telegram API and a temporary AATM database."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "ubuntu"
sys.path.insert(0, str(SCRIPTS))

import aatm_stack  # noqa: E402
import telegram_alerts as alerts  # noqa: E402

TOKEN = "123456:" + "A" * 30
GITHUB_LIKE = "ghp" + "_abcdefghijklmnop"


@pytest.fixture
def telegram():
    received, status = [], SimpleNamespace(code=200)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if status.code == 200:
                received.append({"path": self.path, **body})
            self.send_response(status.code)
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_address[1]}", received=received, status=status)
    server.shutdown()


@pytest.fixture
def world(tmp_path, monkeypatch, telegram):
    db = tmp_path / "aatm.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        "create table arena_sessions(profile_id text, is_open int, phase text, needs_human_reason text);"
        "create table approval_requests(id text, title text, payload text, status text, action_type text);"
        "create table tasks(id text, title text, status text, parent_task_id text, task_type text);"
        "create table projects(id text, name text, status text);"
    )
    connection.commit()
    connection.close()
    bridge = {}
    monkeypatch.setenv("AATM_DB_PATH", str(db))
    monkeypatch.setenv("AATM_READY_FILE", str(tmp_path / "ready.json"))
    monkeypatch.setenv("AATM_WATCHDOG_LOG", str(tmp_path / "watchdog.log"))
    monkeypatch.setattr(aatm_stack, "enabled_windows", lambda: [SimpleNamespace(profile_id="p3", title="Worker 3")])
    monkeypatch.setattr(aatm_stack, "bridge_profiles", lambda *_args: bridge)
    service = alerts.AlertService(alerts.Config(TOKEN, ("11", "22"), telegram.url), state_file=tmp_path / "state" / "t.json")

    def sql(statement, *args):
        with sqlite3.connect(db) as handle:
            handle.execute(statement, args)

    return SimpleNamespace(service=service, bridge=bridge, sql=sql, tmp=tmp_path, sent=telegram.received, telegram=telegram)


def _texts(world):
    return [item["text"] for item in world.sent if item["chat_id"] == "11"]


def test_human_check_alert_reminder_once_and_cleared(world):
    world.service.cycle(1000)
    world.bridge["p3"] = {"status": "needs_human", "needs_human_reason": "page shows: verify you are human"}
    world.service.cycle(1010)
    assert "Worker 3" in _texts(world)[0] and "اضغط Verify في نافذة Worker 3 ثم استئناف الجلسة" in _texts(world)[0]
    assert {item["chat_id"] for item in world.sent} == {"11", "22"} and TOKEN in world.sent[0]["path"]
    world.service.cycle(1100)
    assert len(_texts(world)) == 1
    world.service.cycle(1611)
    world.service.cycle(2500)
    assert len(_texts(world)) == 2 and "تذكير" in _texts(world)[1]
    world.bridge.clear()
    world.service.cycle(2510)
    assert _texts(world)[-1] == "Worker 3: عاد للعمل ✅"


def test_sprint_hold_is_not_reported(world):
    world.sql("insert into arena_sessions values ('p3', 1, 'needs_human', 'held by the owner during the sprint')")
    world.service.cycle(1000)
    world.service.cycle(1010)
    assert world.sent == []


def test_approval_and_finished_or_failed_work_are_reported_once(world):
    world.sql("insert into tasks values ('t1', 'old', 'in_progress', null, 'implementation')")
    world.sql("insert into tasks values ('t2', 'second', 'in_progress', null, 'implementation')")
    world.sql("insert into projects values ('p', 'AATM', 'active')")
    world.service.cycle(1000)
    assert world.sent == []
    world.sql("insert into approval_requests values ('a', 'Merge W1', ?, 'pending', 'merge')", '{"pr_url": "https://x/pull/1"}')
    world.sql("update tasks set status = 'done' where id = 't1'")
    world.sql("update tasks set status = 'failed' where id = 't2'")
    world.sql("update projects set status = 'completed'")
    world.service.cycle(1010)
    world.service.cycle(2000)
    joined = "\n".join(_texts(world))
    assert len(_texts(world)) == 4 and "Merge W1" in joined and "https://x/pull/1" in joined
    assert "old\nانتهى ✅" in joined and "second\nفشل ❌" in joined and "المشروع: AATM" in joined


def test_ready_result_and_watchdog_alerts_are_sent_without_secrets(world):
    log = world.tmp / "watchdog.log"
    log.write_text("old ALERT [service:x] قديم\n")
    world.service.cycle(1000)
    aatm_stack.record_ready("AATM غير جاهز ⚠️ (3/4 متصلين)", ["Worker 3: غير متصل"])
    with log.open("a") as handle:
        handle.write(f"t ALERT [service:backend] failed token={TOKEN} {GITHUB_LIKE}\n")
        handle.write("t ALERT [recovered:service:backend] عاد\nt ALERT [profile:p:needs_human] تكرار\n")
    world.service.cycle(1010)
    texts = _texts(world)
    assert len(texts) == 2 and "3/4" in texts[0] and "Worker 3: غير متصل" in texts[0]
    assert TOKEN not in texts[1] and "ghp_" not in texts[1] and "[محذوف]" in texts[1]


def test_failed_send_is_retried_and_state_file_is_private(world):
    world.service.cycle(1000)
    world.telegram.status.code = 500
    world.bridge["p3"] = {"status": "needs_human", "needs_human_reason": "login"}
    world.service.cycle(1010)
    assert world.sent == []
    world.telegram.status.code = 200
    world.service.cycle(1020)
    assert len(_texts(world)) == 1
    assert stat.S_IMODE(world.service.state_file.stat().st_mode) == 0o600 and TOKEN not in world.service.state_file.read_text()


def test_state_survives_a_restart_without_repeating(world):
    world.bridge["p3"] = {"status": "needs_human", "needs_human_reason": "rate limit reached"}
    world.service.cycle(1000)
    again = alerts.AlertService(world.service.config, state_file=world.service.state_file)
    again.cycle(1010)
    assert len(_texts(world)) == 1 and "انتظر حتى ينتهي الحد" in _texts(world)[0]


@pytest.mark.parametrize(("env", "enabled"), [
    ({"TELEGRAM_BOT_ENABLED": "false", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_CHAT_IDS": "1"}, False),
    ({"TELEGRAM_BOT_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ALLOWED_CHAT_IDS": "1"}, False),
    ({"TELEGRAM_BOT_ENABLED": "true", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_CHAT_IDS": "1, -2"}, True),
])
def test_service_runs_only_when_configured(monkeypatch, tmp_path, env, enabled):
    monkeypatch.setenv("AATM_ENV_FILE", str(tmp_path / "missing"))
    for key in ("TELEGRAM_BOT_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = alerts.load_config()
    assert bool(config) is enabled and (not config or config.chat_ids == ("1", "-2"))


def test_scripts_start_and_stop_the_service():
    start, stop = (SCRIPTS / "start.sh").read_text(), (SCRIPTS / "stop.sh").read_text()
    assert "logs/telegram.pid" in start and 'stop_service telegram "telegram_alerts.py"' in stop
    example = (SCRIPTS.parents[1] / ".env.example").read_text()
    assert "TELEGRAM_BOT_ENABLED=false" in example and "TELEGRAM_BOT_TOKEN=\n" in example
