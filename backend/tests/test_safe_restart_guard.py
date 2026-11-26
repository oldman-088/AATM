"""Owner 2026-11-26: while a safe restart waits, the manager and the peer reviewers take no new job."""

# ruff: noqa: F811 - pytest fixtures are re-exported
from __future__ import annotations

import os
import time

from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures
from test_peer_review import _no_run, _review_job
from test_r7_manager_arena import scripted  # noqa: F401 - fixture

from app.models import ManagerJob
from app.orchestration import restart_guard
from app.orchestration.peer_review import PeerReviewPool


def _flag(tmp_path, monkeypatch, *, age: float = 0.0):
    monkeypatch.setattr(restart_guard, "flag_path", lambda: tmp_path / "restart.pending")
    path = tmp_path / "restart.pending"
    path.write_text("x")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


async def test_no_new_job_starts_while_a_restart_waits(env, scripted, tmp_path, monkeypatch):
    make, project_id = scripted
    page, runtime = make()
    started = _no_run(monkeypatch)
    job_id, _ = await _review_job(env, runtime, project_id)
    _flag(tmp_path, monkeypatch)
    await runtime.drain()
    async with env() as db:
        assert (await db.get(ManagerJob, job_id)).status == "queued"
        assert (await PeerReviewPool().tick(db)).get("restart_pending") is True
    assert started == []


def test_a_stale_flag_does_not_pause_the_team_and_startup_clears_it(tmp_path, monkeypatch):
    path = _flag(tmp_path, monkeypatch, age=restart_guard.MAX_PENDING_SECONDS + 5)
    assert not restart_guard.restart_pending()
    _flag(tmp_path, monkeypatch)
    assert restart_guard.restart_pending()
    restart_guard.clear_restart_flag()
    assert not path.exists() and not restart_guard.restart_pending()
