#!/usr/bin/env python3
"""Helpers for start-all.sh and stop-all.sh: Arena windows, readiness and the ready notification."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAX_WINDOWS = 4
ARENA_AGENT_URL = "https://arena.ai/agent"
CHAT_PATH = re.compile(r"^/agent/[A-Za-z0-9-]+$")


@dataclass(frozen=True)
class Window:
    profile_id: str
    directory: str
    title: str
    url: str


def env_value(name: str, default: str = "") -> str:
    if os.environ.get(name):
        return os.environ[name]
    env_file = Path(os.environ.get("AATM_ENV_FILE", ROOT / ".env"))
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return default


def profiles_home() -> Path:
    return Path.home() / ".aatm-profiles"


def _names(agent_id: str) -> tuple[str, str]:
    if agent_id.startswith("manager"):
        return "manager", "المدير"
    number = agent_id.rsplit("-", 1)[-1]
    return agent_id, f"Worker {number}"


def _order(agent_id: str) -> tuple[int, int, str]:
    number = agent_id.rsplit("-", 1)[-1]
    return (0 if agent_id.startswith("manager") else 1, int(number) if number.isdigit() else 999, agent_id)


def _chat_url(events_json: str | None) -> str | None:
    try:
        events = json.loads(events_json or "[]")
    except ValueError:
        return None
    for event in reversed(events if isinstance(events, list) else []):
        if isinstance(event, dict) and event.get("kind") == "chat_url" and CHAT_PATH.match(str(event.get("detail") or "")):
            return f"https://arena.ai{event['detail']}"
    return None


def enabled_windows() -> list[Window]:
    """Enabled employees with an installed connector, read from AATM's own database (at most four)."""
    db_path = Path(os.environ.get("AATM_DB_PATH", ROOT / "data" / "aatm.db"))
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = list(connection.execute(
            "select p.id profile_id, a.id agent_id from browser_profiles p join agents a on a.id = p.agent_id "
            "where a.is_enabled = 1 and p.connector_token_hash is not null"
        ))
        rows.sort(key=lambda row: _order(row["agent_id"]))
        windows = []
        for row in rows[:MAX_WINDOWS]:
            session = connection.execute(
                "select events from arena_sessions where profile_id = ? and is_open = 1 order by created_at desc limit 1",
                (row["profile_id"],),
            ).fetchone()
            directory, title = _names(row["agent_id"])
            windows.append(Window(row["profile_id"], directory, title, _chat_url(session["events"] if session else None) or ARENA_AGENT_URL))
        return windows
    finally:
        connection.close()


def chrome_main_processes(directory: str | None = None) -> dict[int, str]:
    """Main Chrome processes that use an AATM profile (helpers carry --type= and exit with them)."""
    prefix = f"--user-data-dir={profiles_home()}/"
    found: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if any(arg.startswith("--type=") for arg in argv):
            continue
        for arg in argv:
            if arg.startswith(prefix):
                name = arg[len(prefix):].split("/", 1)[0]
                if directory is None or name == directory:
                    found[int(entry.name)] = name
    return found


def open_windows() -> int:
    chrome = os.environ.get("AATM_CHROME_BIN") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        print("[AATM] ERROR: Google Chrome was not found.", file=sys.stderr)
        return 1
    for window in enabled_windows():
        if chrome_main_processes(window.directory):
            print(f"[AATM] {window.title}: the window is already open.")
            continue
        directory = profiles_home() / window.directory
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [chrome, f"--user-data-dir={directory}", f"--window-name={window.title}", "--no-first-run",
             "--no-default-browser-check", window.url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        print(f"[AATM] {window.title}: opened.")
    return 0


def close_windows(timeout: float = 20.0) -> int:
    processes = chrome_main_processes()
    for pid in processes:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(pid in chrome_main_processes() for pid in processes):
        time.sleep(0.25)
    left = [name for pid, name in chrome_main_processes().items() if pid in processes]
    print(f"[AATM] Closed {len(processes) - len(left)} Arena window(s).")
    if left:
        print(f"[AATM] WARNING: still closing: {', '.join(sorted(left))}", file=sys.stderr)
        return 1
    return 0


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def bridge_profiles(bridge_url: str, token: str) -> dict[str, dict] | None:
    request = urllib.request.Request(f"{bridge_url}/v1/connector/profiles", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return {str(item.get("profile_id")): item for item in json.load(response).get("profiles", [])}
    except (OSError, ValueError, urllib.error.URLError):
        return None


def readiness() -> tuple[int, int, list[str]]:
    windows = enabled_windows()
    problems: list[str] = []
    backend = os.environ.get("AATM_BACKEND_URL", f"http://127.0.0.1:{env_value('BACKEND_PORT', '8000')}")
    if not _http_ok(f"{backend}/api/health"):
        problems.append("الخادم الخلفي لا يستجيب")
    frontend_port = int(env_value("FRONTEND_PORT", "5173"))
    if not _port_open(frontend_port):
        problems.append(f"الواجهة لا تستمع على المنفذ {frontend_port}")
    bridge_url = env_value("ARENA_BRIDGE_URL", "http://127.0.0.1:4100").rstrip("/")
    live = bridge_profiles(bridge_url, env_value("ARENA_BRIDGE_TOKEN"))
    if live is None:
        problems.append("الجسر لا يستجيب")
        live = {}
    connected = 0
    for window in windows:
        state = live.get(window.profile_id) or {}
        if state.get("status") == "needs_human":
            connected += 1 if state.get("connected") else 0
            problems.append(f"{window.title}: يحتاج تدخلك ({state.get('needs_human_reason') or 'فحص بشري'})")
        elif state.get("connected"):
            connected += 1
        else:
            problems.append(f"{window.title}: غير متصل، تأكد من تسجيل الدخول إلى Arena وتفعيل السكربت في نافذته")
    return connected, len(windows), problems


def notify(title: str, body: str = "") -> None:
    sender = shutil.which("notify-send")
    if sender:
        subprocess.run([sender, "--app-name=AATM", title, body], check=False, capture_output=True, timeout=10)


def wait_ready(timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while True:
        connected, total, problems = readiness()
        if not problems and total:
            title = f"AATM جاهز ✅ ({connected}/{total} متصلين)"
            print(f"[AATM] {title}")
            notify(title)
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(float(os.environ.get("AATM_READY_POLL_SECONDS", "3")))
    title = f"AATM غير جاهز ⚠️ ({connected}/{total} متصلين)"
    problems = problems or ["لا يوجد ملف Arena مفعّل"]
    print(f"[AATM] {title}")
    for problem in problems:
        print(f"  - {problem}")
    notify(title, "\n".join(problems))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["open-windows", "close-windows", "wait-ready"])
    args = parser.parse_args()
    if args.command == "open-windows":
        return open_windows()
    if args.command == "close-windows":
        return close_windows()
    return wait_ready(float(os.environ.get("AATM_READY_TIMEOUT", "180")))


if __name__ == "__main__":
    raise SystemExit(main())
