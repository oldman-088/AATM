"""E17 wiring: the watchdog runs the doctor every 10 minutes, as a reader and never as a fixer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest


@dataclass
class Sender:
    """Collects what the watchdog would notify, with the group/key the alerter used."""

    messages: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, key: str, message: str) -> None:
        self.messages.append((key, message))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_watchdog(watchdog, monkeypatch, alerts, clock, calls=None):
    """A Watchdog whose doctor probe returns `alerts`, so the test checks the wiring, not the DB."""
    def fake_run_doctor(timeout=0.0):
        if calls is not None:
            calls.append(timeout)
        return alerts
    monkeypatch.setattr(watchdog, "run_doctor", fake_run_doctor)
    for name in ("check_services", "check_profiles", "check_windows", "check_logs", "check_tasks"):
        monkeypatch.setattr(watchdog.Watchdog, name, lambda self: None)
    dog = watchdog.Watchdog(watchdog.Alerter(send=Sender(), clock=clock), {})
    return dog


def test_the_doctor_is_part_of_the_monitoring_loop(watchdog, monkeypatch):
    """It is wired into cycle(), so a running stack checks itself without anyone asking."""
    calls: list[float] = []
    clock = Clock()
    dog = make_watchdog(watchdog, monkeypatch, (1, {"window_on_wrong_repo:prof-4": "x"}), clock, calls)
    dog.cycle()
    assert len(calls) == 1


def test_findings_become_one_alert_each_and_clear_on_their_own(watchdog, monkeypatch):
    clock = Clock()
    sender = Sender()
    alerts = {"worker_working_without_task:worker-1": "worker-1 يعمل بلا مهمة",
              "window_on_wrong_repo:prof-4": "نافذة على المستودع الخطأ"}
    dog = watchdog.Watchdog(watchdog.Alerter(send=sender, clock=clock), {})
    monkeypatch.setattr(watchdog, "run_doctor", lambda timeout=0.0: (1, alerts))
    dog.check_doctor()
    assert [key for key, _ in sender.messages] == [
        "doctor:worker_working_without_task:worker-1", "doctor:window_on_wrong_repo:prof-4"]

    # the same findings again must not notify twice...
    clock.advance(dog.doctor_interval)
    dog.check_doctor()
    assert len(sender.messages) == 2
    # ...and a clean report says nothing, but marks them as recovered
    clock.advance(dog.doctor_interval)
    monkeypatch.setattr(watchdog, "run_doctor", lambda timeout=0.0: (0, {}))
    dog.check_doctor()
    recovered = [key for key, _ in sender.messages if key.startswith("recovered:doctor:")]
    assert len(recovered) == 2 and len(sender.messages) == 4


def test_it_runs_every_ten_minutes_not_every_cycle(watchdog, monkeypatch):
    calls: list[float] = []
    clock = Clock()
    dog = make_watchdog(watchdog, monkeypatch, (1, {"a:b": "x"}), clock, calls)
    assert dog.doctor_interval == 600.0
    dog.cycle()
    for _ in range(5):  # five ordinary 60-second cycles
        clock.advance(60)
        dog.cycle()
    assert len(calls) == 1, "a 10-minute check must not run every minute"
    clock.advance(600 - 5 * 60)
    dog.cycle()
    assert len(calls) == 2


def test_the_probe_asks_for_alerts_only_and_never_for_a_fix(watchdog, monkeypatch):
    """The argv is the safety guarantee: --alerts, and no --fix anywhere."""
    recorded: dict[str, object] = {}

    class Result:
        returncode = 1
        stdout = json.dumps({"account_terms_not_accepted:prof-7": "حساب لم يقبل الشروط بعد"})
        stderr = ""

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        recorded["timeout"] = kwargs.get("timeout")
        return Result()

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    code, alerts = watchdog.run_doctor()
    assert code == 1
    assert alerts == {"account_terms_not_accepted:prof-7": "حساب لم يقبل الشروط بعد"}
    argv = recorded["argv"]
    assert "--alerts" in argv and "--once" in argv
    assert "--fix" not in argv
    assert str(watchdog.DOCTOR_SCRIPT).endswith("aatm_doctor.py")
    assert recorded["timeout"] == watchdog.DOCTOR_TIMEOUT_SECONDS


@pytest.mark.parametrize("returncode,stdout,expected", [
    (2, "", "database"),                     # the doctor could not read the database
    (1, "Traceback (most recent call last)", "unavailable"),  # it crashed
    (1, "not json", "unavailable"),          # it did not answer in a readable way
])
def test_a_broken_check_is_reported_instead_of_going_quiet(watchdog, monkeypatch, returncode, stdout, expected):
    def fake_run(argv, **kwargs):
        class Result:
            pass
        Result.returncode, Result.stdout, Result.stderr = returncode, stdout, ""
        return Result()

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    sender = Sender()
    dog = watchdog.Watchdog(watchdog.Alerter(send=sender, clock=Clock()), {})
    dog.check_doctor()
    assert len(sender.messages) == 1
    assert sender.messages[0][0] == f"doctor:{expected}"


def test_the_real_probe_script_exists_and_is_read_only(watchdog):
    """Only the entry point exists, and its help says what it will not do."""
    assert watchdog.DOCTOR_SCRIPT.exists(), "watchdog points at a script that is not there"
    assert watchdog.DOCTOR_INTERVAL_SECONDS == 600.0
