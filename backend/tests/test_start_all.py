"""start-all.sh / stop-all.sh with mocked docker, Chrome, notifications and local services."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "ubuntu"
BRIDGE_TOKEN = "test-bridge-value"


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _database(path: Path, workers: dict[str, bool]) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        "create table agents(id text, is_enabled int);"
        "create table browser_profiles(id text, agent_id text, connector_token_hash text);"
        "create table arena_sessions(profile_id text, is_open int, events text, created_at text);"
    )
    for agent, enabled in workers.items():
        db.execute("insert into agents values (?, ?)", (agent, int(enabled)))
        db.execute("insert into browser_profiles values (?, ?, 'hash')", (f"profile-{agent}", agent))
    db.commit()
    db.close()


class _Services:
    def __init__(self, connected: set[str]) -> None:
        owner = self
        self.connected = connected

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_GET(self) -> None:
                if self.path == "/v1/connector/profiles" and self.headers.get("Authorization") != f"Bearer {BRIDGE_TOKEN}":
                    self.send_response(401)
                    self.end_headers()
                    return
                data = {"status": "ok"}
                if self.path == "/v1/connector/profiles":
                    data = {"profiles": [{"profile_id": p, "connected": True, "status": "ready"} for p in owner.connected]}
                body = json.dumps(data).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def stack(tmp_path):
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    (tmp_path / "home").mkdir()
    _executable(bin_dir / "docker", f'''echo "docker $*" >> "{log}"
if [[ "$1" == inspect ]]; then echo false; fi''')
    _executable(bin_dir / "chrome", f'printf "chrome %s\\n" "$*" >> "{log}"')
    _executable(bin_dir / "notify-send", f'printf "notify %s\\n" "$*" >> "{log}"')
    _executable(bin_dir / "fake-start", f'echo start >> "{log}"')
    _executable(bin_dir / "fake-stop", f'echo stop >> "{log}"')

    def build(workers: dict[str, bool], connected: set[str]):
        services = _Services(connected)
        db = tmp_path / "aatm.db"
        _database(db, workers)
        env = {**os.environ, "HOME": str(tmp_path / "home"), "PATH": f"{bin_dir}:{os.environ['PATH']}",
               "AATM_PYTHON": sys.executable, "AATM_DB_PATH": str(db), "AATM_CHROME_BIN": str(bin_dir / "chrome"),
               "AATM_START_SCRIPT": str(bin_dir / "fake-start"), "AATM_STOP_SCRIPT": str(bin_dir / "fake-stop"),
               "AATM_ENV_FILE": str(tmp_path / "missing.env"), "AATM_READY_TIMEOUT": "0", "AATM_READY_POLL_SECONDS": "0.05",
               "AATM_WINDOW_SETTLE_SECONDS": "0", "AATM_WINDOW_RETRY_SECONDS": "0",
               "AATM_READY_FILE": str(tmp_path / "ready-status.json"),
               "AATM_BACKEND_URL": f"http://127.0.0.1:{services.port}", "FRONTEND_PORT": str(services.port),
               "ARENA_BRIDGE_URL": f"http://127.0.0.1:{services.port}", "ARENA_BRIDGE_TOKEN": BRIDGE_TOKEN}
        return env, log

    return build


def _run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPTS / script), *args], env=env, capture_output=True, text=True, timeout=30)


def test_start_all_starts_stopped_services_and_reports_ready(stack):
    env, log = stack({"manager-0": True, "worker-1": True}, {"profile-manager-0", "profile-worker-1"})
    result = _run("start-all.sh", env)
    calls = log.read_text()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docker start aatm-redis" in calls and "docker start aatm-rabbitmq" in calls and "start\n" in calls
    assert "--window-name=المدير" in calls and "--window-name=Worker 1" in calls and "https://arena.ai/agent" in calls
    assert f"--user-data-dir={env['HOME']}/.aatm-profiles/worker-1" in calls
    assert "notify --app-name=AATM AATM جاهز ✅ (2/2 متصلين)" in calls
    # Every line start-all prints itself is Arabic, and the dashboard link comes with the result.
    assert f"[AATM] اللوحة: http://localhost:{env['FRONTEND_PORT']}" in result.stdout
    assert "[AATM] المدير: فُتحت." in result.stdout and "opened." not in result.stdout


def test_disabled_profiles_stay_closed_and_at_most_four_windows_open(stack):
    workers = {"manager-0": True, **{f"worker-{n}": True for n in range(1, 6)}, "worker-6": False}
    env, log = stack(workers, {f"profile-{a}" for a in ("manager-0", "worker-1", "worker-2", "worker-3")})
    assert _run("start-all.sh", env).returncode == 0
    opened = [line for line in log.read_text().splitlines() if line.startswith("chrome ")]
    assert sum(line.endswith("about:blank") for line in opened) == 4 and not any("worker-4" in line or "worker-5" in line or "worker-6" in line for line in opened)


def test_missing_connection_is_named_in_terminal_and_notification(stack):
    env, log = stack({"manager-0": True, "worker-1": True}, {"profile-manager-0"})
    result = _run("start-all.sh", env)
    assert result.returncode == 1 and "AATM غير جاهز ⚠️ (1/2 متصلين)" in result.stdout and "Worker 1: غير متصل" in result.stdout
    assert f"[AATM] اللوحة: http://localhost:{env['FRONTEND_PORT']}" in result.stdout
    assert "Worker 1: غير متصل" in log.read_text()


def test_open_task_window_returns_to_its_arena_chat(stack, tmp_path):
    env, log = stack({"worker-1": True}, {"profile-worker-1"})
    db = sqlite3.connect(env["AATM_DB_PATH"])
    db.execute("insert into arena_sessions values ('profile-worker-1', 1, ?, '2026')",
               (json.dumps([{"kind": "chat_url", "detail": "/agent/abc-123"}]),))
    db.commit()
    db.close()
    assert _run("start-all.sh", env).returncode == 0
    assert "https://arena.ai/agent/abc-123" in log.read_text()


def _fake_browser(*args: str) -> subprocess.Popen:
    code = "import signal,sys,time; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); print('up', flush=True); time.sleep(30)"
    process = subprocess.Popen([sys.executable, "-c", code, *args], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "up"
    return process


@pytest.mark.parametrize("flag", [False, True])
def test_stop_all_closes_only_aatm_windows_and_containers_only_on_request(stack, flag):
    env, log = stack({"worker-1": True}, set())
    home = env["HOME"]
    ours = _fake_browser(f"--user-data-dir={home}/.aatm-profiles/worker-1")
    other = _fake_browser("--user-data-dir=/tmp/personal-chrome")
    helper = _fake_browser(f"--user-data-dir={home}/.aatm-profiles/worker-1", "--type=renderer")
    try:
        result = _run("stop-all.sh", env, *(["--containers"] if flag else []))
        assert result.returncode == 0, result.stderr
        assert ours.wait(timeout=5) == 0
        time.sleep(0.1)
        assert other.poll() is None and helper.poll() is None
        assert "stop\n" in log.read_text()
        assert ("docker stop aatm-redis aatm-rabbitmq" in log.read_text()) is flag
        assert ("توقف AATM والحاويتان" if flag else "Redis وRabbitMQ ما زالا يعملان") in result.stdout
    finally:
        for process in (ours, other, helper):
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)


def test_stop_scripts_never_force_kill_and_frontend_pid_is_the_real_server():
    assert "kill -9" not in (SCRIPTS / "stop.sh").read_text() and "kill -9" not in (SCRIPTS / "stop-all.sh").read_text()
    assert "./node_modules/.bin/vite" in (SCRIPTS / "start.sh").read_text()


def _stack_module():
    spec = importlib.util.spec_from_file_location("aatm_stack_under_test", SCRIPTS / "aatm_stack.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_chrome_rewritten_command_line_is_recognized_but_shell_text_is_not():
    stack_module = _stack_module()
    rewritten = b"/opt/google/chrome/chrome --user-data-dir=/h/.aatm-profiles/worker-1 --window-name=Worker 1 https://arena.ai/agent\0"
    assert "--user-data-dir=/h/.aatm-profiles/worker-1" in stack_module.command_line_args(rewritten)
    shell = b"bash\0-c\0echo --user-data-dir=/h/.aatm-profiles/worker-1\0"
    assert "--user-data-dir=/h/.aatm-profiles/worker-1" not in stack_module.command_line_args(shell)


def test_arena_loads_only_after_every_window_has_started(stack):
    env, log = stack({"manager-0": True, "worker-1": True}, {"profile-manager-0", "profile-worker-1"})
    env["AATM_WINDOW_SETTLE_SECONDS"] = "0.5"  # the fake browsers log from separate processes
    assert _run("start-all.sh", env).returncode == 0
    targets = [line.rsplit(" ", 1)[-1] for line in log.read_text().splitlines() if line.startswith("chrome ")]
    assert sorted(targets[:2]) == ["about:blank", "about:blank"]
    assert targets[2:] == ["https://arena.ai/agent", "https://arena.ai/agent"]


def test_unconnected_window_is_loaded_again_only_once(stack):
    env, log = stack({"manager-0": True, "worker-1": True}, {"profile-manager-0"})
    env["AATM_READY_TIMEOUT"] = "0.5"
    env["AATM_WINDOW_SETTLE_SECONDS"] = "0.3"
    assert _run("start-all.sh", env).returncode == 1
    worker = [line for line in log.read_text().splitlines() if line.startswith("chrome ") and "worker-1" in line]
    assert sorted(line.rsplit(" ", 1)[-1] for line in worker) == ["about:blank", "https://arena.ai/agent", "https://arena.ai/agent"]
    manager = [line for line in log.read_text().splitlines() if line.startswith("chrome ") and "/manager " in line]
    assert len(manager) == 2
