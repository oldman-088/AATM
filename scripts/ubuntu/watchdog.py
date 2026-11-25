#!/usr/bin/env python3
"""AATM watchdog — read-only monitoring loop (started by scripts/ubuntu/watchdog.sh).

It mostly *observes*: HTTP GETs, TCP connects, ``docker inspect``, reading log files and opening the
SQLite database with ``mode=ro``. It never restarts a service and never writes to the database.

The one repair it makes (E56) is to load Arena again in a window that has stayed disconnected from the
bridge for 3 minutes — the same step ``start-all`` takes once. Loading the page only re-runs the
userscript; nothing is typed or sent to Arena. A window whose Chrome is closed is opened again. Each
window gets at most 3 tries, 10 minutes apart; after that the owner gets one alert. The counter resets
when the window connects. ``AATM_WATCHDOG_REOPEN=0`` turns this off.

Every interval it checks the backend health endpoint, the bridge and frontend ports, the Redis and
RabbitMQ containers, each Arena profile's state (via the bridge admin API, token read from .env and
never printed), new error lines in logs/backend.log and logs/bridge.log, and tasks with no progress
for more than 20 minutes. Each new problem becomes a desktop notification (notify-send) and a
timestamped line in logs/watchdog.log; the same alert is not repeated within 10 minutes, and a
single "recovered" notification is sent when a problem clears.

Every 10 minutes it also runs ``scripts/ubuntu/aatm_doctor.py --alerts`` (E17) and turns its
findings into the same kind of alert — drifted workers, open sessions, a window on the wrong
repository (E34) and accounts still waiting on the Terms of Service (E25). The watchdog asks for a
report and nothing else: it never passes ``--fix``, so it does not write to the database or to a
browser. The doctor runs as a subprocess for the same reason the watchdog runs without the venv —
it must still report when the app itself is broken.

Standard library only, so it also runs without the project virtualenv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
TITLE = "AATM — تنبيه"
RECOVERED = "عاد للعمل"
DEDUP_SECONDS = 600
STUCK_SECONDS = 20 * 60
DOCTOR_SCRIPT = ROOT_DIR / "scripts" / "ubuntu" / "aatm_doctor.py"
DOCTOR_INTERVAL_SECONDS = 600.0  # the 10-minute consistency check (E17)
DOCTOR_TIMEOUT_SECONDS = 120.0
ACTIVE_TASK_STATUSES = ("in_progress", "running", "working")
CONTAINERS = ("aatm-redis", "aatm-rabbitmq")
SERVICE_GRACE_SECONDS = 90.0   # a normal restart finishes within this time
PROFILE_GRACE_SECONDS = 180.0  # Arena windows reconnect by themselves within this time
REOPEN_COOLDOWN_SECONDS = 600.0  # time between two reloads of the same window (E56)
REOPEN_MAX_TRIES = 3             # after this many reloads the owner is asked to look at the window
STACK_SCRIPT = ROOT_DIR / "scripts" / "ubuntu" / "aatm_stack.py"
LOG_PATTERNS = re.compile(r"(?:^|\s)ERROR[:\s]|(?i:orchestration[^\n]*fail|tick failed|job \S+ failed)")
ACCESS_LINE = re.compile(r'"[A-Z]+ \S+ HTTP/[\d.]+" (\d{3})')
SHUTDOWN_ERRORS = re.compile(r"^(?:asyncio\.(?:exceptions\.)?)?CancelledError|^KeyboardInterrupt|^SystemExit|ConnectionClosed")
TRACEBACK_LINKS = ("During handling of the above exception", "The above exception was the direct cause")
_VOLATILE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|0x[0-9a-f]+|\d+(?:\.\d+)?", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


# --------------------------------------------------------------------------------------------- alerts
@dataclass
class Alerter:
    """De-duplicates alerts: a key is sent at most once per ``dedup_seconds``.

    *State* problems (``set_problems``) are tracked so their clearing sends one recovery message.
    *Events* (``event``, e.g. a log line) have no recovery; they are only rate-limited.
    """

    send: Callable[[str, str], None]
    dedup_seconds: float = DEDUP_SECONDS
    clock: Callable[[], float] = time.monotonic
    last_sent: dict[str, float] = field(default_factory=dict)
    active: dict[str, str] = field(default_factory=dict)

    def _emit(self, key: str, message: str) -> bool:
        now = self.clock()
        last = self.last_sent.get(key)
        if last is not None and now - last < self.dedup_seconds:
            return False
        self.last_sent[key] = now
        self.send(key, message)
        return True

    def event(self, key: str, message: str) -> bool:
        return self._emit("event:" + key, message)

    def set_problems(self, group: str, problems: dict[str, str]) -> None:
        """Replace the current problems of one check group (``key -> message``)."""
        prefix = group + ":"
        current = {prefix + k: v for k, v in problems.items()}
        for key, message in current.items():
            if key not in self.active:
                self._emit(key, message)
        for key in [k for k in self.active if k.startswith(prefix) and k not in current]:
            old = self.active.pop(key)
            self._emit("recovered:" + key, f"{RECOVERED}: {old}")
        self.active.update(current)


# --------------------------------------------------------------------------------------------- probes
def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def http_get(url: str, token: str | None = None, timeout: float = 4.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


def container_running(name: str) -> bool | None:
    """True/False, or None when docker itself is unavailable."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.returncode == 0 and out.stdout.strip() == "true"


class LogTail:
    """Returns only lines appended since the last call; starts at the current end of the file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.offset = path.stat().st_size
        except OSError:
            self.offset = 0

    restarted = False

    def new_lines(self) -> list[str]:
        self.restarted = False
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:  # the log was truncated (service restarted by start.sh)
            self.offset = 0
            self.restarted = True
        if size == self.offset:
            return []
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read(min(size - self.offset, 2_000_000))
        self.offset += len(data)
        return data.decode("utf-8", "replace").splitlines()


class LogScanner:
    """Turns new log lines into alerts: one per traceback (its final error), none for normal traffic."""

    def __init__(self) -> None:
        self.in_traceback = False
        self.pending: str | None = None

    def _flush(self, out: list[str]) -> None:
        if self.pending is not None and not SHUTDOWN_ERRORS.search(self.pending):
            out.append(self.pending)
        self.pending = None

    def feed(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        for line in lines:
            text = line.strip()
            if text.startswith(TRACEBACK_LINKS):
                self.pending = None  # a chained error: the last one in the chain is reported
                continue
            if text.startswith("Traceback (most recent call last)"):
                self.pending = None
                self.in_traceback = True
                continue
            if self.in_traceback:
                if not text or line[:1] in " \t":
                    continue
                self.in_traceback = False
                self.pending = text
                continue
            if self.pending is not None:
                if not text:
                    continue
                self._flush(out)
            access = ACCESS_LINE.search(line)
            if access:
                if int(access.group(1)) >= 500:
                    out.append(text)
            elif LOG_PATTERNS.search(line) and "Exception in ASGI application" not in line:
                out.append(text)
        if not lines:
            self._flush(out)
        return out


def log_key(line: str) -> str:
    """A stable de-duplication key for a log line: numbers, ids and timestamps removed."""
    return _VOLATILE.sub("#", line.strip())[:160]


def _parse_db_path(env: dict[str, str]) -> Path:
    url = env.get("SYNC_DATABASE_URL") or env.get("DATABASE_URL") or ""
    m = re.match(r"sqlite(?:\+\w+)?:///(.+)$", url)
    if not m:
        return ROOT_DIR / "data" / "aatm.db"
    raw = m.group(1)
    p = Path(raw)
    if not p.is_absolute():
        # Relative SQLite URLs in .env are resolved by the backend from backend/ or the root;
        # prefer the one that exists.
        for base in (ROOT_DIR, ROOT_DIR / "backend"):
            if (base / raw).exists():
                return (base / raw).resolve()
        return (ROOT_DIR / raw).resolve()
    return p


def stuck_tasks(db_path: Path, now_utc: datetime, stuck_seconds: float = STUCK_SECONDS) -> dict[str, str]:
    """Tasks in an active status whose ``updated_at`` is older than ``stuck_seconds`` (read-only)."""
    if not db_path.exists():
        return {}
    marks = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                f"SELECT id, title, status, updated_at, progress_percent FROM tasks WHERE status IN ({marks})",
                ACTIVE_TASK_STATUSES,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    found: dict[str, str] = {}
    for task_id, title, status, updated_at, progress in rows:
        try:
            ts = datetime.fromisoformat(str(updated_at))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)  # the backend stores UTC
        age = (now_utc - ts).total_seconds()
        if age > stuck_seconds:
            found[str(task_id)] = (
                f"مهمة عالقة {int(age // 60)} دقيقة بلا تقدم: «{str(title)[:60]}» ({status}, {progress}%)"
            )
    return found


# --------------------------------------------------------------------------------------------- doctor
def run_doctor(timeout: float = DOCTOR_TIMEOUT_SECONDS) -> tuple[int, dict[str, str]]:
    """One read-only doctor pass: ``({key: message}, exit code)``.

    Only ``--alerts`` is passed, and never ``--fix``: a monitoring loop that repaired state by itself
    would be a different, much more dangerous program. ``(1, {})`` means the doctor could not run or
    did not answer in a readable way; the caller turns that into an alert of its own.
    """
    python = os.environ.get("AATM_PYTHON") or sys.executable
    try:
        out = subprocess.run(
            [python, str(DOCTOR_SCRIPT), "--alerts", "--once"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, {}
    if not out.stdout.strip():
        return out.returncode or 1, {}
    try:
        alerts = json.loads(out.stdout)
    except ValueError:
        return 1, {}
    if not isinstance(alerts, dict):
        return 1, {}
    return out.returncode, {str(k): str(v) for k, v in alerts.items() if isinstance(v, str)}


# --------------------------------------------------------------------------------------------- watchdog
def load_stack():
    """Import the sibling aatm_stack.py by path (the scripts are not a package)."""
    import importlib.util

    name = "aatm_stack_for_watchdog"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, STACK_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Watchdog:
    def __init__(self, alerter: Alerter, env: dict[str, str]) -> None:
        self.alerter = alerter
        self.env = env
        self.backend_port = int(os.environ.get("BACKEND_PORT", "8000"))
        self.bridge_port = int(os.environ.get("ARENA_BRIDGE_PORT") or env.get("ARENA_BRIDGE_PORT") or 4100)
        self.frontend_port = int(os.environ.get("FRONTEND_PORT", "5173"))
        self.db_path = _parse_db_path(env)
        self.logs = {
            name: LogTail(ROOT_DIR / "logs" / f"{name}.log") for name in ("backend", "bridge")
        }
        self.seen_connected: set[str] = set()
        self.scanners = {name: LogScanner() for name in self.logs}
        self.quiet_until: dict[str, float] = {}
        self.first_seen: dict[str, float] = {}
        self.doctor_interval = float(os.environ.get("AATM_DOCTOR_INTERVAL", DOCTOR_INTERVAL_SECONDS))
        self.next_doctor = 0.0  # the first cycle runs it right away: this is the after-boot pass
        self.reopen_enabled = os.environ.get("AATM_WATCHDOG_REOPEN", "1") != "0"
        self.missing_since: dict[str, float] = {}
        self.reopen_tries: dict[str, int] = {}
        self.last_reopen: dict[str, float] = {}
        self._stack = None

    def _settled(self, group: str, problems: dict[str, str], grace: float) -> dict[str, str]:
        """Only problems that outlasted a normal restart or reconnect are reported."""
        now = self.alerter.clock()
        keys = {f"{group}:{key}" for key in problems}
        self.first_seen = {k: v for k, v in self.first_seen.items() if not k.startswith(group + ":") or k in keys}
        for key in keys:
            self.first_seen.setdefault(key, now)
        return {key: text for key, text in problems.items() if now - self.first_seen[f"{group}:{key}"] >= grace}

    @property
    def _token(self) -> str:
        return (os.environ.get("ARENA_BRIDGE_TOKEN") or self.env.get("ARENA_BRIDGE_TOKEN") or "").strip()

    def check_services(self) -> None:
        problems: dict[str, str] = {}
        status, _ = http_get(f"http://127.0.0.1:{self.backend_port}/api/health")
        if status != 200:
            problems["backend"] = f"الخادم الخلفي لا يستجيب (/api/health → {status or 'لا اتصال'})"
        if not port_open(self.bridge_port):
            problems["bridge"] = f"الجسر لا يستمع على 127.0.0.1:{self.bridge_port}"
        if not port_open(self.frontend_port):
            problems["frontend"] = f"الواجهة لا تستمع على المنفذ {self.frontend_port}"
        for name in CONTAINERS:
            running = container_running(name)
            if running is None:
                problems["docker"] = "تعذّر تشغيل docker للتحقق من الحاويات"
            elif not running:
                problems[name] = f"الحاوية {name} لا تعمل"
        self.alerter.set_problems("service", self._settled("service", problems, SERVICE_GRACE_SECONDS))

    def check_profiles(self) -> None:
        token = self._token
        if not port_open(self.bridge_port):
            return  # already reported by check_services; keep the previous profile problems
        status, body = http_get(f"http://127.0.0.1:{self.bridge_port}/v1/connector/profiles", token or None)
        problems: dict[str, str] = {}
        if status in (401, 403):
            problems["auth"] = f"الجسر رفض رمز ARENA_BRIDGE_TOKEN ({status})"
        elif status == 200:
            try:
                profiles = json.loads(body).get("profiles") or []
            except (ValueError, AttributeError):
                profiles = []
            for p in profiles:
                pid = str(p.get("profile_id") or "?")
                if p.get("status") == "needs_human":
                    reason = p.get("needs_human_reason") or "بدون سبب"
                    problems[f"{pid}:needs_human"] = f"الملف {pid} يحتاج تدخلاً بشرياً: {reason}"
                if p.get("connected"):
                    self.seen_connected.add(pid)
                elif pid in self.seen_connected:
                    reason = p.get("last_error") or p.get("agent_state") or p.get("status") or "?"
                    problems[f"{pid}:offline"] = f"انقطع اتصال الملف {pid} (السبب: {reason})"
        elif status:
            problems["api"] = f"واجهة ملفات الجسر أعادت {status}"
        offline = {k: v for k, v in problems.items() if k.endswith(":offline")}
        settled = self._settled("profile", offline, PROFILE_GRACE_SECONDS)
        problems = {k: v for k, v in problems.items() if k not in offline or k in settled}
        self.alerter.set_problems("profile", problems)

    def check_logs(self) -> None:
        now = self.alerter.clock()
        for name, tail in self.logs.items():
            lines = tail.new_lines()
            if tail.restarted:
                self.quiet_until[name] = now + SERVICE_GRACE_SECONDS
            alerts = self.scanners[name].feed(lines)
            if now < self.quiet_until.get(name, 0.0):
                continue  # messages from a normal restart
            for line in alerts:
                self.alerter.event(f"{name}:{log_key(line)}", f"{name}.log: {line[:200]}")

    def check_tasks(self) -> None:
        self.alerter.set_problems("task", stuck_tasks(self.db_path, datetime.now(UTC)))

    def check_doctor(self) -> None:
        """Every 10 minutes: the drifted-state report (E17), as alerts. Read-only by construction."""
        now = self.alerter.clock()
        if now < self.next_doctor:
            return
        self.next_doctor = now + self.doctor_interval
        code, alerts = run_doctor()
        if code == 0:
            self.alerter.set_problems("doctor", {})  # clean: clears any finding that has gone away
            return
        if code == 2:
            self.alerter.set_problems("doctor", {"database": "فحص aatm doctor لم يستطع قراءة قاعدة البيانات"})
            return
        if code == 1 and alerts:
            self.alerter.set_problems("doctor", alerts)
            return
        # it did not run at all (no python, no script, a crash, a timeout): say so instead of going quiet
        self.alerter.set_problems("doctor", {"unavailable": "فحص aatm doctor لم يعمل؛ راجع scripts/ubuntu/aatm_doctor.py"})

    def stack(self):
        """scripts/ubuntu/aatm_stack.py, loaded once: it knows the windows, their folders and URLs."""
        if self._stack is None:
            self._stack = load_stack()
        return self._stack

    def check_windows(self) -> None:
        """E56: load Arena again in a window that stays disconnected, so nobody has to do it by hand."""
        if not self.reopen_enabled:
            return
        stack = self.stack()
        _, _, _, missing = stack.readiness()  # `missing` is empty while the bridge is down
        now = self.alerter.clock()
        ids = {w.profile_id for w in missing}
        for pid in [p for p in self.missing_since if p not in ids]:  # connected again: start over
            self.missing_since.pop(pid, None)
            self.reopen_tries.pop(pid, None)
            self.last_reopen.pop(pid, None)
        problems: dict[str, str] = {}
        chrome = stack._chrome_binary()
        for window in missing:
            pid = window.profile_id
            since = self.missing_since.setdefault(pid, now)
            tries = self.reopen_tries.get(pid, 0)
            if tries >= REOPEN_MAX_TRIES:
                problems[pid] = (f"{window.title}: لم يتصل بعد {tries} محاولات إعادة تحميل؛ "
                                 "افتح نافذته وتأكد من تسجيل الدخول إلى Arena وتفعيل السكربت")
                continue
            if not chrome or now - since < PROFILE_GRACE_SECONDS:
                continue
            if now - self.last_reopen.get(pid, -REOPEN_COOLDOWN_SECONDS) < REOPEN_COOLDOWN_SECONDS:
                continue
            running = bool(stack.chrome_main_processes(window.directory))
            stack._launch(chrome, window, window.url)
            self.reopen_tries[pid] = tries + 1
            self.last_reopen[pid] = now
            how = "أعيد تحميل صفحة Arena" if running else "أعيد فتح النافذة"
            write_log(f"[watchdog] {window.title} ({pid}) غير متصل: {how} (المحاولة {tries + 1}/{REOPEN_MAX_TRIES})")
        self.alerter.set_problems("window", problems)

    def cycle(self) -> None:
        for check in (self.check_services, self.check_profiles, self.check_windows, self.check_logs,
                      self.check_tasks, self.check_doctor):
            try:
                check()
            except Exception as exc:  # a broken probe must never stop the watchdog
                write_log(f"[watchdog] probe {check.__name__} failed: {type(exc).__name__}")


# --------------------------------------------------------------------------------------------- output
LOG_FILE = Path(os.environ.get("AATM_WATCHDOG_LOG") or ROOT_DIR / "logs" / "watchdog.log")


def write_log(line: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"{_now_iso()} {line}\n")


def notify(key: str, message: str) -> None:
    write_log(f"ALERT [{key}] {message}")
    try:
        subprocess.run(
            ["notify-send", "--app-name=AATM", "-u", "normal", TITLE, message[:250]],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=float(os.environ.get("WATCHDOG_INTERVAL", "60")))
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--test-notify", action="store_true", help="send one test notification and exit")
    parser.add_argument("--no-doctor", action="store_true", help="skip the 10-minute aatm doctor pass")
    args = parser.parse_args(argv)

    if args.test_notify:
        notify("test", "إشعار تجريبي من مراقب AATM — يعمل بشكل صحيح")
        return 0

    def _on_sigterm(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)
    env = read_env(ROOT_DIR / ".env")
    dog = Watchdog(Alerter(send=notify), env)
    if args.no_doctor:
        dog.check_doctor = lambda: None  # the read-only monitoring stays, the doctor pass does not
    write_log(f"[watchdog] started (pid {os.getpid()}, interval {args.interval:.0f}s)")
    try:
        while True:
            dog.cycle()
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    write_log("[watchdog] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
