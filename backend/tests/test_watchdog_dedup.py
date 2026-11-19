"""scripts/ubuntu/watchdog.py: alert de-duplication, recovery notices and read-only probes."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ubuntu" / "watchdog.py"


@pytest.fixture(scope="module")
def wd():
    spec = importlib.util.spec_from_file_location("aatm_watchdog", _PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _alerter(wd):
    sent: list[tuple[str, str]] = []
    clock = FakeClock()
    alerter = wd.Alerter(send=lambda key, msg: sent.append((key, msg)), clock=clock)
    return alerter, sent, clock


def test_same_event_is_sent_at_most_once_per_10_minutes(wd):
    alerter, sent, clock = _alerter(wd)
    assert alerter.event("backend:x", "ERROR x") is True
    clock.t += 60
    assert alerter.event("backend:x", "ERROR x") is False
    clock.t += 9 * 60 - 61  # still inside the 10-minute window
    assert alerter.event("backend:x", "ERROR x") is False
    assert alerter.event("backend:y", "ERROR y") is True  # a different alert is not suppressed
    clock.t += 62  # now past 10 minutes since the first send
    assert alerter.event("backend:x", "ERROR x") is True
    assert [k for k, _ in sent] == ["event:backend:x", "event:backend:y", "event:backend:x"]


def test_persistent_problem_alerts_once_and_recovery_once(wd):
    alerter, sent, clock = _alerter(wd)
    for _ in range(30):  # 30 cycles of the same outage
        alerter.set_problems("service", {"backend": "backend down"})
        clock.t += 60
    assert [m for _, m in sent] == ["backend down"]

    alerter.set_problems("service", {})
    alerter.set_problems("service", {})
    assert len(sent) == 2
    assert sent[1][0] == "recovered:service:backend"
    assert sent[1][1].startswith(wd.RECOVERED)


def test_flapping_problem_is_not_repeated_within_10_minutes(wd):
    alerter, sent, clock = _alerter(wd)
    alerter.set_problems("profile", {"p1:offline": "p1 offline"})
    clock.t += 60
    alerter.set_problems("profile", {})  # recovered
    clock.t += 60
    alerter.set_problems("profile", {"p1:offline": "p1 offline"})  # back again after 2 minutes
    clock.t += 60
    alerter.set_problems("profile", {})
    assert [k for k, _ in sent] == ["profile:p1:offline", "recovered:profile:p1:offline"]
    clock.t += 10 * 60
    alerter.set_problems("profile", {"p1:offline": "p1 offline"})
    assert sent[-1][0] == "profile:p1:offline"


def test_groups_are_independent(wd):
    alerter, sent, _ = _alerter(wd)
    alerter.set_problems("service", {"bridge": "bridge down"})
    alerter.set_problems("task", {})  # clearing another group must not "recover" the service problem
    assert [k for k, _ in sent] == ["service:bridge"]


def test_log_key_ignores_numbers_and_ids(wd):
    a = wd.log_key("2026-11-17 10:00:01 ERROR task 1f2e3d4c-aaaa-bbbb-cccc-111122223333 failed after 3s")
    b = wd.log_key("2026-11-17 10:07:45 ERROR task 9a8b7c6d-dddd-eeee-ffff-444455556666 failed after 12s")
    assert a == b


def test_log_tail_reads_only_new_lines_and_handles_truncation(wd, tmp_path):
    log = tmp_path / "backend.log"
    log.write_text("old ERROR line\n")
    tail = wd.LogTail(log)
    assert tail.new_lines() == []  # existing content is not reported
    with log.open("a") as fh:
        fh.write("Traceback (most recent call last):\n")
    assert tail.new_lines() == ["Traceback (most recent call last):"]
    log.write_text("x\n")  # restarted service truncates the log
    assert tail.new_lines() == ["x"]


def test_stuck_tasks_reads_database_read_only(wd, tmp_path):
    db = tmp_path / "aatm.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, updated_at TEXT, progress_percent INT)")
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=25)).replace(tzinfo=None).isoformat(sep=" ")
    fresh = (now - timedelta(minutes=5)).replace(tzinfo=None).isoformat(sep=" ")
    conn.executemany(
        "INSERT INTO tasks VALUES (?,?,?,?,?)",
        [("t1", "stuck", "in_progress", old, 40), ("t2", "fresh", "in_progress", fresh, 10),
         ("t3", "done", "completed", old, 100)],
    )
    conn.commit()
    conn.close()
    before = db.read_bytes()
    found = wd.stuck_tasks(db, now)
    assert list(found) == ["t1"]
    assert "25" in found["t1"]
    assert db.read_bytes() == before


REDIS_TRACE = [
    "Traceback (most recent call last):", '  File "a.py", line 1, in x', "    await fail(error)", "    raise error",
    "TimeoutError: first", "", "During handling of the above exception, another exception occurred:", "",
    "Traceback (most recent call last):", '  File "b.py", line 2, in y', "    raise error",
    "redis.exceptions.TimeoutError: Timeout reading", "",
]


def test_traceback_gives_one_alert_with_its_final_error(wd):
    scanner = wd.LogScanner()
    assert scanner.feed(REDIS_TRACE) == []
    assert scanner.feed([]) == ["redis.exceptions.TimeoutError: Timeout reading"]


@pytest.mark.parametrize("line", [
    'INFO:     127.0.0.1:1 - "GET /api/health HTTP/1.1" 401 Unauthorized',
    'INFO:     127.0.0.1:1 - "GET /api/v1/x?limit=500 HTTP/1.1" 200 OK',
    "    await fail(error)", "raise error",
])
def test_normal_lines_stay_quiet(wd, line):
    scanner = wd.LogScanner()
    assert scanner.feed([line]) == [] and scanner.feed([]) == []


def test_shutdown_tracebacks_stay_quiet_but_real_errors_alert(wd):
    scanner = wd.LogScanner()
    assert scanner.feed(["Traceback (most recent call last):", "  x", "asyncio.exceptions.CancelledError"]) == []
    assert scanner.feed([]) == []
    assert scanner.feed(['INFO: 1 - "GET /x HTTP/1.1" 500 Internal Server Error']) != []
    assert scanner.feed(["ERROR:    something broke"]) == ["ERROR:    something broke"]
    assert scanner.feed(["Background orchestration cycle failed; the next interval will retry"]) != []


def _monitor(wd, monkeypatch, now):
    sent = []
    alerter = wd.Alerter(send=lambda key, message: sent.append(key), clock=lambda: now[0])
    monkeypatch.setattr(wd, "_parse_db_path", lambda _env: Path("/nonexistent"))
    return wd.Watchdog(alerter, {}), sent


def test_short_outage_is_quiet_and_a_lasting_one_alerts(wd, monkeypatch):
    now = [0.0]
    monitor, sent = _monitor(wd, monkeypatch, now)
    monkeypatch.setattr(wd, "http_get", lambda *_a, **_k: (0, b""))
    monkeypatch.setattr(wd, "port_open", lambda *_a, **_k: True)
    monkeypatch.setattr(wd, "container_running", lambda _name: True)
    monitor.check_services()
    now[0] = 60.0
    monitor.check_services()
    assert sent == []
    now[0] = 91.0
    monitor.check_services()
    assert sent == ["service:backend"]


def test_log_lines_right_after_a_restart_are_ignored(wd, monkeypatch, tmp_path):
    now = [0.0]
    monitor, sent = _monitor(wd, monkeypatch, now)
    log = tmp_path / "backend.log"
    log.write_text("old old old old\n")
    monitor.logs = {"backend": wd.LogTail(log)}
    monitor.scanners = {"backend": wd.LogScanner()}
    log.write_text("ERROR: a\n")
    monitor.check_logs()
    assert sent == []
    now[0] = 100.0
    with log.open("a") as handle:
        handle.write("ERROR: b\n")
    monitor.check_logs()
    assert len(sent) == 1
