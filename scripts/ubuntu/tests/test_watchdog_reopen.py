"""E56: the watchdog loads Arena again in a window that stays disconnected, instead of waiting for a person."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Window:
    profile_id: str
    directory: str
    title: str
    url: str


MANAGER = Window("p-manager", "manager", "Manager", "https://arena.ai/agent")


@dataclass
class FakeStack:
    """Stands in for aatm_stack.py: no bridge, no Chrome, no database."""

    missing: list[Window] = field(default_factory=list)
    running: bool = True
    chrome: str | None = "/usr/bin/google-chrome"
    launched: list[tuple[str, str]] = field(default_factory=list)

    def readiness(self):
        return 0, 1, [], list(self.missing)

    def _chrome_binary(self):
        return self.chrome

    def chrome_main_processes(self, directory=None):
        return {123: directory} if self.running else {}

    def _launch(self, chrome, window, target):
        self.launched.append((window.directory, target))

    def restart_window(self, chrome, window):
        self.launched.append((window.directory, "restart"))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(watchdog, stack):
    sent: list[tuple[str, str]] = []
    clock = Clock()
    dog = watchdog.Watchdog(watchdog.Alerter(send=lambda k, m: sent.append((k, m)), clock=clock), {})
    dog.reopen_enabled = True
    dog._stack = stack
    return dog, clock, sent


def test_a_short_disconnect_is_left_alone(watchdog):
    stack = FakeStack(missing=[MANAGER])
    dog, clock, _ = make(watchdog, stack)
    dog.check_windows()
    clock.now += watchdog.PROFILE_GRACE_SECONDS - 1
    dog.check_windows()
    assert stack.launched == []


def test_a_window_disconnected_past_the_grace_time_is_reloaded_once_per_cooldown(watchdog):
    stack = FakeStack(missing=[MANAGER])
    dog, clock, sent = make(watchdog, stack)
    dog.check_windows()
    clock.now += watchdog.PROFILE_GRACE_SECONDS
    dog.check_windows()
    assert stack.launched == [("manager", "https://arena.ai/agent")]
    clock.now += 60  # the next ordinary cycle
    dog.check_windows()
    assert len(stack.launched) == 1, "a reload must wait for the cooldown"
    clock.now += watchdog.REOPEN_COOLDOWN_SECONDS
    dog.check_windows()
    assert len(stack.launched) == 2
    assert sent == []  # reloading is quiet; only giving up is an alert


def test_after_the_last_try_the_owner_gets_one_alert(watchdog):
    stack = FakeStack(missing=[MANAGER])
    dog, clock, sent = make(watchdog, stack)
    dog.check_windows()
    clock.now += watchdog.PROFILE_GRACE_SECONDS
    for _ in range(watchdog.REOPEN_MAX_TRIES + 3):
        dog.check_windows()
        clock.now += watchdog.REOPEN_COOLDOWN_SECONDS
    assert len(stack.launched) == watchdog.REOPEN_MAX_TRIES
    assert [k for k, _ in sent] == ["window:p-manager"]


def test_connecting_again_resets_the_tries_and_clears_the_alert(watchdog):
    stack = FakeStack(missing=[MANAGER])
    dog, clock, sent = make(watchdog, stack)
    dog.check_windows()
    clock.now += watchdog.PROFILE_GRACE_SECONDS
    for _ in range(watchdog.REOPEN_MAX_TRIES + 1):
        dog.check_windows()
        clock.now += watchdog.REOPEN_COOLDOWN_SECONDS
    stack.missing = []
    dog.check_windows()
    assert [k for k, _ in sent] == ["window:p-manager", "recovered:window:p-manager"]
    assert dog.reopen_tries == {} and dog.missing_since == {}


def test_a_closed_chrome_is_opened_again(watchdog):
    stack = FakeStack(missing=[MANAGER], running=False)
    dog, clock, _ = make(watchdog, stack)
    dog.check_windows()
    clock.now += watchdog.PROFILE_GRACE_SECONDS
    dog.check_windows()
    assert stack.launched == [("manager", "https://arena.ai/agent")]
    assert "أعيد فتح النافذة" in (watchdog.LOG_FILE.read_text(encoding="utf-8"))


def test_it_can_be_turned_off_and_does_nothing_without_chrome(watchdog):
    stack = FakeStack(missing=[MANAGER], chrome=None)
    dog, clock, _ = make(watchdog, stack)
    dog.check_windows()
    clock.now += watchdog.PROFILE_GRACE_SECONDS
    dog.check_windows()
    assert stack.launched == []
    dog.reopen_enabled = False
    stack.chrome = "/usr/bin/google-chrome"
    dog.check_windows()
    assert stack.launched == []


def test_the_real_stack_script_is_found(watchdog):
    stack = watchdog.load_stack()
    for name in ("readiness", "_launch", "_chrome_binary", "chrome_main_processes"):
        assert callable(getattr(stack, name))


def test_a_reload_that_did_not_reconnect_is_followed_by_a_full_window_restart(watchdog):
    """2026-11-26: Worker 4 stayed disconnected after a reload; restarting its Chrome brought it back."""
    stack = FakeStack(missing=[MANAGER])
    dog, clock, _ = make(watchdog, stack)
    dog.check_windows()
    clock.now += watchdog.PROFILE_GRACE_SECONDS
    dog.check_windows()
    clock.now += watchdog.REOPEN_COOLDOWN_SECONDS
    dog.check_windows()
    assert stack.launched == [("manager", "https://arena.ai/agent"), ("manager", "restart")]
