"""P0 / E37: a busy or disconnected manager window must not consume job attempts (bounded refund)."""

from __future__ import annotations

from app.models import ManagerJob
from app.orchestration.manager_arena import MAX_ATTEMPT_REFUNDS, MAX_JOB_ATTEMPTS, ManagerArenaRuntime


def _job(attempts: int) -> ManagerJob:
    return ManagerJob(kind="review", status="running", attempts=attempts, context={})


def test_refund_returns_the_attempt():
    job = _job(1)
    ManagerArenaRuntime._refund_attempt(job)
    assert job.attempts == 0
    assert job.context["attempt_refunds"] == 1


def test_busy_forever_never_reaches_the_attempt_limit_until_refunds_are_spent():
    job = _job(0)
    for _ in range(MAX_ATTEMPT_REFUNDS):
        job.attempts += 1                      # what _run_job does before each try
        ManagerArenaRuntime._refund_attempt(job)
        assert job.attempts < MAX_JOB_ATTEMPTS
    job.attempts += 1                          # refunds are spent: attempts now count again, so no endless loop
    ManagerArenaRuntime._refund_attempt(job)
    assert job.attempts == 1


def test_refund_never_goes_negative():
    job = _job(0)
    ManagerArenaRuntime._refund_attempt(job)
    assert job.attempts == 0
