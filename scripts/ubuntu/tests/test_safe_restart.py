"""Owner 2026-11-26: a backend restart waits for running reviews instead of cutting them off."""

from __future__ import annotations

import sqlite3

from conftest import load_script

safe = load_script("safe_restart.py", "safe_restart_under_test")


def _db(tmp_path, statuses):
    path = tmp_path / "aatm.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE manager_jobs (id TEXT, kind TEXT, status TEXT)")
    conn.executemany("INSERT INTO manager_jobs VALUES (?, 'review', ?)", [(f"job-{i}-xxxx", s) for i, s in enumerate(statuses)])
    conn.commit()
    conn.close()
    return path


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_it_waits_while_a_review_runs_and_goes_ahead_when_it_ends(tmp_path):
    path = _db(tmp_path, ["running", "queued"])
    flag = tmp_path / "restart.pending"
    clock = Clock()

    def sleep(seconds):
        assert flag.exists(), "no new review may start while it waits"
        clock.sleep(seconds)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE manager_jobs SET status = 'done' WHERE status = 'running'")
        conn.commit()
        conn.close()

    assert safe.wait_until_quiet(path, flag, timeout=60, poll=10, clock=clock, sleep=sleep, log=lambda m: None)
    assert clock.t == 10


def test_on_timeout_nothing_restarts_and_the_flag_is_removed(tmp_path):
    path = _db(tmp_path, ["running"])
    flag = tmp_path / "restart.pending"
    clock = Clock()
    assert not safe.wait_until_quiet(path, flag, timeout=30, poll=10, clock=clock, sleep=clock.sleep, log=lambda m: None)
    assert not flag.exists(), "a restart that gave up must not leave the reviews paused"


def test_a_database_without_manager_jobs_is_quiet(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert safe.running_jobs(path) == []
