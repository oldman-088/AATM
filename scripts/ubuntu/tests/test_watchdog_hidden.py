"""E78: a window that stays minimised or hidden is opened again on screen, then the owner is asked.

On 2026-11-26 seven of nine Arena windows were hidden and their follow-ups failed with page_hidden.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Window:
    profile_id: str
    directory: str
    title: str
    url: str


WORKER = Window("profile-worker-5", "profile-worker-5", "Worker 5", "https://arena.ai/agent")


@dataclass
class FakeStack:
    restarted: list[str] = field(default_factory=list)

    def _chrome_binary(self):
        return "/usr/bin/google-chrome"

    def enabled_windows(self):
        return [WORKER]

    def _launch(self, chrome, window, target):
        pass

    def restart_window(self, chrome, window):
        self.restarted.append(window.profile_id)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeBridge:
    def __init__(self) -> None:
        self.profile = {"profile_id": WORKER.profile_id, "status": "ready", "connected": True,
                        "agent_state": "ready", "page_hidden": True, "page_hidden_seconds": 200.0,
                        "in_flight": False}

    def get(self, url, token=None, timeout=4.0):
        return 200, json.dumps({"profiles": [self.profile]}).encode()


def make(watchdog, monkeypatch):
    bridge = FakeBridge()
    alerts: list[str] = []
    monkeypatch.setattr(watchdog, "http_get", bridge.get)
    monkeypatch.setattr(watchdog, "port_open", lambda port: True)
    monkeypatch.setattr(watchdog, "write_log", lambda line: None)
    clock = Clock()
    dog = watchdog.Watchdog(watchdog.Alerter(send=lambda k, m: alerts.append(m), clock=clock), {})
    dog.reopen_enabled = True
    dog._stack = FakeStack()
    return dog, clock, bridge, alerts


def test_a_window_hidden_past_the_grace_time_is_restarted_once_per_cooldown(watchdog, monkeypatch):
    dog, clock, _, _ = make(watchdog, monkeypatch)
    dog.check_human_windows()
    assert dog._stack.restarted == [WORKER.profile_id]
    clock.now += 60
    dog.check_human_windows()
    assert len(dog._stack.restarted) == 1
    clock.now += watchdog.HIDDEN_COOLDOWN_SECONDS
    dog.check_human_windows()
    assert len(dog._stack.restarted) == 2


def test_a_short_hide_or_a_running_command_is_left_alone(watchdog, monkeypatch):
    dog, _, bridge, _ = make(watchdog, monkeypatch)
    bridge.profile["page_hidden_seconds"] = 10.0
    dog.check_human_windows()
    bridge.profile.update(page_hidden_seconds=500.0, in_flight=True)
    dog.check_human_windows()
    assert dog._stack.restarted == []


def test_after_the_last_try_the_owner_is_asked_and_showing_it_clears_the_count(watchdog, monkeypatch):
    dog, clock, bridge, alerts = make(watchdog, monkeypatch)
    for _ in range(watchdog.REOPEN_MAX_TRIES + 1):
        dog.check_human_windows()
        clock.now += watchdog.HIDDEN_COOLDOWN_SECONDS + 1
    assert len(dog._stack.restarted) == watchdog.REOPEN_MAX_TRIES
    assert any("مخفية" in a for a in alerts)
    bridge.profile.update(page_hidden=False, page_hidden_seconds=None)
    dog.check_human_windows()
    assert dog.hidden_restarts == {}


def test_a_human_check_window_is_never_restarted(watchdog, monkeypatch):
    dog, _, bridge, _ = make(watchdog, monkeypatch)
    bridge.profile["status"] = "needs_human"
    monkeypatch.setattr(watchdog, "http_post", lambda *a, **k: (500, b""))
    dog.check_human_windows()
    assert dog._stack.restarted == []
