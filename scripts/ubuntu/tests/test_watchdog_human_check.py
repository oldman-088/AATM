"""E57: a window marked "needs a human" is resumed by itself once the page shows no check any more.

The watchdog never touches a check: it only reads the page's own selector test, resumes a clean
page, and reloads a page that still shows a check a few times before leaving it to the owner.
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


MANAGER = Window("profile-manager", "manager", "Manager", "https://arena.ai/agent")


@dataclass
class FakeStack:
    launched: list[str] = field(default_factory=list)

    def _chrome_binary(self):
        return "/usr/bin/google-chrome"

    def enabled_windows(self):
        return [MANAGER]

    def _launch(self, chrome, window, target):
        self.launched.append(window.profile_id)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeBridge:
    def __init__(self, check_found: bool, status: str = "needs_human") -> None:
        self.check_found = check_found
        self.status = status
        self.posts: list[str] = []

    def get(self, url, token=None, timeout=4.0):
        return 200, json.dumps({"profiles": [{"profile_id": "profile-manager", "status": self.status,
                                              "connected": True}]}).encode()

    def post(self, url, token=None, body=None, timeout=40.0):
        self.posts.append(url.rsplit("/", 1)[-1])
        if url.endswith("/command"):
            return 200, json.dumps({"result": {"human_check": {"found": self.check_found},
                                               "state": "needs_human" if self.check_found else "ready"}}).encode()
        self.status = "ready"
        return 200, b"{}"


def make(watchdog, monkeypatch, bridge):
    monkeypatch.setattr(watchdog, "http_get", bridge.get)
    monkeypatch.setattr(watchdog, "http_post", bridge.post)
    monkeypatch.setattr(watchdog, "port_open", lambda port: True)
    monkeypatch.setattr(watchdog, "write_log", lambda line: None)
    clock = Clock()
    dog = watchdog.Watchdog(watchdog.Alerter(send=lambda k, m: None, clock=clock), {})
    dog.reopen_enabled = True
    dog._stack = FakeStack()
    return dog, clock


def test_a_page_that_no_longer_shows_a_check_is_resumed(watchdog, monkeypatch):
    bridge = FakeBridge(check_found=False)
    dog, _ = make(watchdog, monkeypatch, bridge)
    dog.check_human_windows()
    assert bridge.posts == ["command", "reset"]
    assert dog._stack.launched == []


def test_a_page_that_still_shows_a_check_is_never_resumed_only_reloaded_a_few_times(watchdog, monkeypatch):
    bridge = FakeBridge(check_found=True)
    dog, clock = make(watchdog, monkeypatch, bridge)
    for _ in range(40):
        dog.check_human_windows()
        clock.now += watchdog.HUMAN_RECHECK_SECONDS
    assert "reset" not in bridge.posts
    assert dog._stack.launched == ["profile-manager"] * watchdog.HUMAN_RELOAD_MAX


def test_the_page_is_asked_at_most_once_per_recheck_interval(watchdog, monkeypatch):
    bridge = FakeBridge(check_found=True)
    dog, clock = make(watchdog, monkeypatch, bridge)
    dog.check_human_windows()
    clock.now += 30
    dog.check_human_windows()
    assert bridge.posts.count("command") == 1


def test_a_ready_window_is_left_alone(watchdog, monkeypatch):
    bridge = FakeBridge(check_found=False, status="ready")
    dog, _ = make(watchdog, monkeypatch, bridge)
    dog.check_human_windows()
    assert bridge.posts == []


class BlankBridge(FakeBridge):
    def get(self, url, token=None, timeout=4.0):
        return 200, json.dumps({"profiles": [{"profile_id": "profile-manager", "status": "ready",
                                              "connected": True, "agent_state": "offline"}]}).encode()


def test_e59_a_connected_window_without_a_composer_is_opened_again_after_the_grace_time(watchdog, monkeypatch):
    bridge = BlankBridge(check_found=False, status="ready")
    dog, clock = make(watchdog, monkeypatch, bridge)
    dog.check_human_windows()
    assert dog._stack.launched == []
    clock.now += watchdog.BLANK_GRACE_SECONDS
    dog.check_human_windows()
    assert dog._stack.launched == ["profile-manager"]
    for _ in range(20):
        clock.now += watchdog.REOPEN_COOLDOWN_SECONDS
        dog.check_human_windows()
    assert len(dog._stack.launched) == watchdog.REOPEN_MAX_TRIES
    assert bridge.posts == [], "a blank page is never resumed or tested, only opened again"
