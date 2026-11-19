#!/usr/bin/env python3
"""AATM Telegram alerts: send-only. It opens no port, reads no commands and changes nothing in AATM."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aatm_stack  # noqa: E402

REPEAT_SECONDS = 600.0
POLL_SECONDS = 15.0
MAX_TEXT = 700
MAX_OUTBOX = 50
HELD_BY_OWNER = "held by the owner"
FINISHED = {"done", "merged", "completed"}
SECRET_PATTERNS = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b([a-z_]*(?:token|password|passwd|secret|api[_-]?key)[a-z_]*)\s*[=:]\s*\S+"),
)
WATCHDOG_ALERT = re.compile(r"ALERT \[(service|event|task):[^\]]*\] (.*)$")


def clean(text: Any, limit: int = 300) -> str:
    """One short line with anything that looks like a credential removed."""
    value = " ".join(str(text or "").replace("```", " ").split())
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[محذوف]", value)
    return value[:limit]


def human_action(title: str, reason: str) -> str:
    lowered = reason.lower()
    if re.search(r"verify|human|robot|captcha|check element|complete the check", lowered):
        return f"اضغط Verify في نافذة {title} ثم استئناف الجلسة"
    if re.search(r"sign in|log in|login", lowered):
        return f"سجّل الدخول في نافذة {title} ثم استئناف الجلسة"
    if "terms" in lowered:
        return f"راجع الشروط واقبلها في نافذة {title} ثم استئناف الجلسة"
    if re.search(r"rate limit|too many requests|unusual traffic", lowered):
        return f"انتظر حتى ينتهي الحد في نافذة {title} ثم استئناف الجلسة"
    return f"افتح نافذة {title} وتحقق منها ثم استئناف الجلسة"


@dataclass(frozen=True)
class Config:
    token: str
    chat_ids: tuple[str, ...]
    api_base: str = "https://api.telegram.org"


def load_config() -> Config | None:
    if aatm_stack.env_value("TELEGRAM_BOT_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return None
    token = aatm_stack.env_value("TELEGRAM_BOT_TOKEN")
    chats = tuple(c for c in re.split(r"[,\s]+", aatm_stack.env_value("TELEGRAM_ALLOWED_CHAT_IDS")) if re.fullmatch(r"-?\d+", c))
    if not token or not chats:
        return None
    return Config(token, chats, aatm_stack.env_value("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"))


def send_telegram(config: Config, chat_id: str, text: str) -> bool:
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    request = urllib.request.Request(f"{config.api_base}/bot{config.token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):  # the URL holds the token, so the error text is never logged
        return False


def default_state_file() -> Path:
    return Path(os.environ.get("AATM_TELEGRAM_STATE", Path.home() / ".aatm" / "telegram-state.json"))


@dataclass
class AlertService:
    config: Config
    state_file: Path = field(default_factory=default_state_file)
    send: Callable[[Config, str, str], bool] = send_telegram
    state: dict[str, Any] = field(default_factory=dict)
    outbox: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        try:
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        for name in ("sent", "human", "tasks", "projects"):
            self.state.setdefault(name, {})
        self.state.setdefault("approvals", [])

    # ------------------------------------------------------------------ delivery
    def push(self, key: str, text: str, now: float) -> None:
        if now - float(self.state["sent"].get(key, -1e12)) < REPEAT_SECONDS:
            return
        self.state["sent"][key] = now
        self.outbox = (self.outbox + [{"chat": chat, "text": text[:MAX_TEXT]} for chat in self.config.chat_ids])[-MAX_OUTBOX:]

    def flush(self) -> None:
        self.outbox = [item for item in self.outbox if not self.send(self.config, item["chat"], item["text"])]

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_file.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle)
        os.replace(temporary, self.state_file)

    # ------------------------------------------------------------------ sources
    @staticmethod
    def _database() -> sqlite3.Connection:
        path = Path(os.environ.get("AATM_DB_PATH", aatm_stack.ROOT / "data" / "aatm.db"))
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def open_human_checks(self, db: sqlite3.Connection) -> dict[str, str]:
        checks: dict[str, str] = {}
        for row in db.execute("select profile_id, needs_human_reason from arena_sessions where is_open = 1 and phase = 'needs_human'"):
            reason = str(row["needs_human_reason"] or "")
            if not reason.startswith(HELD_BY_OWNER):
                checks[row["profile_id"]] = reason
        bridge = aatm_stack.bridge_profiles(aatm_stack.env_value("ARENA_BRIDGE_URL", "http://127.0.0.1:4100").rstrip("/"),
                                            aatm_stack.env_value("ARENA_BRIDGE_TOKEN")) or {}
        for profile_id, item in bridge.items():
            if item.get("status") == "needs_human":
                checks[profile_id] = str(item.get("needs_human_reason") or "فحص بشري")
        return checks

    # ------------------------------------------------------------------ checks
    def check_humans(self, db: sqlite3.Connection, now: float) -> None:
        titles = {window.profile_id: window.title for window in aatm_stack.enabled_windows()}
        checks = self.open_human_checks(db)
        known = self.state["human"]
        for profile_id, raw in checks.items():
            title = titles.get(profile_id, profile_id)
            reason = clean(raw, 160) or "فحص بشري"
            entry = known.get(profile_id)
            details = f"السبب: {reason}\nالمطلوب: {human_action(title, reason)}"
            if entry is None:
                known[profile_id] = {"since": now, "reminded": False}
                self.push(f"human:{profile_id}:{reason}", f"⚠️ {title} يحتاجك\n{details}", now)
            elif not entry["reminded"] and now - entry["since"] >= REPEAT_SECONDS:
                entry["reminded"] = True
                self.push(f"reminder:{profile_id}", f"⏰ تذكير: {title} ما زال ينتظرك\n{details}", now)
        for profile_id in [p for p in known if p not in checks]:
            del known[profile_id]
            self.push(f"cleared:{profile_id}", f"{titles.get(profile_id, profile_id)}: عاد للعمل ✅", now)

    def check_approvals(self, db: sqlite3.Connection, now: float, first: bool) -> None:
        rows = list(db.execute("select id, title, payload from approval_requests where status = 'pending' and action_type = 'merge'"))
        seen = set(self.state["approvals"])
        for row in rows:
            if row["id"] in seen or first:
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            link = clean(payload.get("pr_url") or payload.get("url") or "", 200)
            self.push(f"approval:{row['id']}", f"🔔 طلب دمج ينتظر موافقتك\n{clean(row['title'], 200)}\n{link}".strip(), now)
        self.state["approvals"] = [row["id"] for row in rows]

    def _transitions(self, name: str, rows: list[sqlite3.Row], first: bool, label: str, now: float) -> None:
        known = self.state[name]
        for row in rows:
            status = row["status"]
            changed = known.get(row["id"]) != status
            known[row["id"]] = status
            if first or not changed or (status not in FINISHED and status != "failed"):
                continue
            mark = "فشل ❌" if status == "failed" else "انتهى ✅"
            self.push(f"{name}:{row['id']}:{status}", f"{label}: {clean(row['title'], 200)}\n{mark}", now)

    def check_work(self, db: sqlite3.Connection, now: float, first: bool) -> None:
        tasks = list(db.execute("select id, title, status from tasks where parent_task_id is null and task_type = 'implementation'"))
        self._transitions("tasks", tasks, first, "المهمة", now)
        projects = list(db.execute("select id, name as title, status from projects"))
        self._transitions("projects", projects, first, "المشروع", now)

    def check_ready(self, now: float, first: bool) -> None:
        try:
            ready = json.loads(aatm_stack.ready_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        stamp = ready.get("ts")
        if stamp == self.state.get("ready_ts"):
            return
        self.state["ready_ts"] = stamp
        if not first:
            lines = [clean(ready.get("title"), 120), *[f"- {clean(p, 160)}" for p in (ready.get("problems") or [])[:6]]]
            self.push(f"ready:{stamp}", "\n".join(lines), now)

    def check_watchdog(self, now: float, first: bool) -> None:
        path = Path(os.environ.get("AATM_WATCHDOG_LOG", aatm_stack.ROOT / "logs" / "watchdog.log"))
        try:
            size = path.stat().st_size
        except OSError:
            return
        offset = int(self.state.get("watchdog_offset", size if first else 0))
        offset = 0 if size < offset else offset
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        complete = data.rfind(b"\n") + 1
        self.state["watchdog_offset"] = offset + complete
        for line in data[:complete].decode("utf-8", "replace").splitlines():
            match = WATCHDOG_ALERT.search(line)
            if match:
                text = clean(match.group(2))
                self.push(f"watchdog:{re.sub(r'[0-9]+', '#', text)[:120]}", f"🚨 تنبيه من المراقب\n{text}", now)

    def cycle(self, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        first = not self.state.get("initialized")
        db = self._database()
        try:
            for check in (lambda: self.check_humans(db, moment), lambda: self.check_approvals(db, moment, first),
                          lambda: self.check_work(db, moment, first)):
                try:
                    check()
                except (sqlite3.Error, OSError, KeyError, ValueError) as exc:
                    print(f"[telegram] check failed: {type(exc).__name__}", flush=True)
        finally:
            db.close()
        self.check_ready(moment, first)
        self.check_watchdog(moment, first)
        self.state["initialized"] = True
        self.flush()
        self.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-enabled", action="store_true")
    args = parser.parse_args()
    config = load_config()
    if args.check_enabled:
        return 0 if config else 1
    if config is None:
        print("[telegram] disabled or not configured", flush=True)
        return 0
    running = [True]
    signal.signal(signal.SIGTERM, lambda *_: running.clear())
    service = AlertService(config)
    print("[telegram] started", flush=True)
    while running:
        try:
            service.cycle()
        except (sqlite3.Error, OSError) as exc:
            print(f"[telegram] cycle failed: {type(exc).__name__}", flush=True)
        deadline = time.monotonic() + POLL_SECONDS
        while running and time.monotonic() < deadline:
            time.sleep(0.5)
    print("[telegram] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
