"""Owner 2026-11-26: an idle worker reviews pull requests in parallel with the manager."""

# ruff: noqa: F811 - pytest fixtures are re-exported
from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures
from test_r7_manager_arena import scripted  # noqa: F401 - fixture

from app.db.base import utcnow
from app.models import Agent, ArenaSession, ManagerJob, Task
from app.orchestration import peer_review as peer_module
from app.orchestration.manager_arena import CLAIMED_JOBS
from app.orchestration.peer_review import PEER_SESSION_KIND, PeerReviewer, PeerReviewPool

VERDICT = json.dumps({"pr_number": 7, "verdict": "approve", "summary_ar": "التغيير سليم والاختبارات تغطيه.",
                      "comments": []})


async def _review_job(env, runtime, project_id, *, author="worker-1") -> tuple[str, str]:
    async with env() as db:
        parent = Task(project_id=project_id, title="Add login", status="in_review", task_type="implementation",
                      assigned_agent_id=author, pr_url="https://github.com/acme/demo/pull/7", pr_number=7)
        db.add(parent)
        await db.flush()
        review = Task(project_id=project_id, title="Review login", status="in_progress", task_type="review",
                      assigned_agent_id="manager-0", parent_task_id=parent.id)
        db.add(review)
        for agent in (await db.execute(select(Agent).where(Agent.role == "worker"))).scalars():
            agent.status, agent.current_task_id = "idle", None
        await db.commit()
        job = await runtime.enqueue(db, "review", project_id=project_id, review_task_id=review.id,
                                    context={"pr_number": 7, "parent_task_id": parent.id})
        return job.id, review.id


def _no_run(monkeypatch) -> list[tuple[str, str]]:
    started: list[tuple[str, str]] = []

    async def run(self, job_id):
        started.append((self.agent_id, job_id))
        return {"status": "done"}

    monkeypatch.setattr(PeerReviewer, "run", run)
    return started


async def test_an_idle_worker_takes_the_review_but_never_its_own_pr(env, scripted, monkeypatch):
    make, project_id = scripted
    _page, runtime = make()
    started = _no_run(monkeypatch)
    job_id, review_id = await _review_job(env, runtime, project_id, author="worker-1")
    pool = PeerReviewPool()
    async with env() as db:
        result = await pool.tick(db)
    await asyncio.gather(*pool.running.values())
    assert len(started) == 1 and started[0][1] == job_id and started[0][0] != "worker-1"
    assert result["started"][0]["worker"] == started[0][0]
    async with env() as db:
        reviewer = await db.get(Agent, started[0][0])
        assert reviewer.status == "working" and reviewer.current_task_id == review_id
        assert (await db.get(Task, review_id)).lock_owner == reviewer.id
    assert job_id in CLAIMED_JOBS
    CLAIMED_JOBS.discard(job_id)


async def test_a_worker_whose_window_drives_its_own_work_is_not_taken(env, scripted, monkeypatch):
    make, project_id = scripted
    _page, runtime = make()
    started = _no_run(monkeypatch)
    monkeypatch.setattr(peer_module.get_settings(), "PEER_REVIEW_MAX", 1)
    job_id, _ = await _review_job(env, runtime, project_id, author="worker-1")
    async with env() as db:
        for agent in (await db.execute(select(Agent).where(Agent.role == "worker"))).scalars():
            if agent.id != "worker-2":
                agent.status = "offline"
        worker = await db.get(Agent, "worker-2")
        db.add(ArenaSession(agent_id=worker.id, profile_id=worker.browser_profile_id, phase="reviewing", page_state="idle",
                            is_open=True, events=[]))  # no recorded chat: cannot be parked
        await db.commit()
    async with env() as db:
        await PeerReviewPool().tick(db)
    assert started == [] and job_id not in CLAIMED_JOBS


async def test_the_manager_skips_a_job_a_peer_has_claimed(env, scripted):
    make, project_id = scripted
    page, runtime = make({"REVIEW": [VERDICT]})
    job_id, _ = await _review_job(env, runtime, project_id)
    CLAIMED_JOBS.add(job_id)
    try:
        await runtime.drain()
        async with env() as db:
            assert (await db.get(ManagerJob, job_id)).status == "queued"
    finally:
        CLAIMED_JOBS.discard(job_id)


async def test_a_peer_review_gives_the_verdict_and_frees_the_worker(env, scripted, monkeypatch):
    from app.orchestration import arena_runner as arena_runner_module
    from app.orchestration import review as review_module
    from app.orchestration import scheduler as scheduler_module

    make, project_id = scripted
    page, runtime = make({"REVIEW": [VERDICT]})
    submitted: list[dict[str, Any]] = []

    async def material(db, parent):
        return {"changed_files": [{"filename": "app/auth.py", "status": "modified", "additions": 1, "deletions": 0}],
                "mechanical_discrepancies": []}

    async def compare(project, session):
        return {"ok": True, "files": [{"filename": "app/auth.py", "patch": "+check_token(token)\n"}]}

    async def submit_review(db, review, reviewer, *, event, body):
        submitted.append({"reviewer": reviewer.id, "event": event, "body": body})
        return {"follow_up_task_id": None, "merge_approval_id": None, "event": event}

    async def complete_task(db, task, *, summary, target_status):
        task.status = target_status

    monkeypatch.setattr(arena_runner_module.arena_runner, "review_material", material)
    monkeypatch.setattr(arena_runner_module.arena_runner, "_compare", compare)
    monkeypatch.setattr(review_module.review_service, "submit_review", submit_review)
    monkeypatch.setattr(scheduler_module.task_scheduler, "complete_task", complete_task)
    job_id, review_id = await _review_job(env, runtime, project_id)
    async with env() as db:
        worker = await db.get(Agent, "worker-3")
        worker.status, worker.current_task_id = "working", review_id
        await db.commit()
        profile_id = worker.browser_profile_id
    reviewer = PeerReviewer("worker-3", profile_id, adapter=page, session_factory=env)
    reviewer.poll_seconds = reviewer.start_grace_seconds = reviewer.retry_backoff_seconds = 0
    reviewer.busy_poll_seconds = 0.01
    CLAIMED_JOBS.add(job_id)
    await reviewer.run(job_id)
    assert job_id not in CLAIMED_JOBS
    assert submitted and submitted[0]["reviewer"] == "worker-3" and "Peer review" in submitted[0]["body"]
    async with env() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "done" and job.result["reviewer"] == "worker-3"
        assert (await db.get(Agent, "worker-3")).status == "idle"
        sessions = list((await db.execute(select(ArenaSession).where(ArenaSession.kind == PEER_SESSION_KIND))).scalars())
        assert sessions and not any(s.is_open for s in sessions), "the review chat is closed when the review ends"


async def test_a_peer_review_without_a_verdict_goes_back_to_the_manager(env, scripted, monkeypatch):
    make, project_id = scripted
    page, runtime = make()
    job_id, _ = await _review_job(env, runtime, project_id)

    async def broken(self, db, job):
        job.status = "needs_human"
        job.error = "invalid JSON after one retry"
        job.finished_at = utcnow()
        await db.commit()
        return {"job_id": job.id, "status": job.status}

    monkeypatch.setattr(PeerReviewer, "_run_job", broken)
    async with env() as db:
        profile_id = (await db.get(Agent, "worker-3")).browser_profile_id
    await PeerReviewer("worker-3", profile_id, adapter=page, session_factory=env).run(job_id)
    async with env() as db:
        job = await db.get(ManagerJob, job_id)
        assert job.status == "queued" and job.context["peer_failed"] == "worker-3"
    started = _no_run(monkeypatch)
    async with env() as db:
        await PeerReviewPool().tick(db)
    assert started == [], "a job a peer could not finish is left to the manager"


async def test_a_failed_peer_review_with_a_fallback_job_is_not_reviewed_twice(env, scripted, monkeypatch):
    """Live 2026-11-26: the peer's failure queued the short fallback review AND the original was requeued."""
    make, project_id = scripted
    page, runtime = make()
    job_id, _ = await _review_job(env, runtime, project_id)

    async def fails_with_fallback(self, db, job):
        assert await self._fallback_review(db, job, "invalid JSON", raw="x")
        return {"job_id": job.id, "status": job.status}

    monkeypatch.setattr(PeerReviewer, "_run_job", fails_with_fallback)
    async with env() as db:
        profile_id = (await db.get(Agent, "worker-4")).browser_profile_id
    await PeerReviewer("worker-4", profile_id, adapter=page, session_factory=env).run(job_id)
    async with env() as db:
        jobs = list((await db.execute(select(ManagerJob).where(ManagerJob.kind == "review"))).scalars())
        queued = [j for j in jobs if j.status == "queued"]
        assert (await db.get(ManagerJob, job_id)).status == "failed"
        assert len(queued) == 1 and queued[0].context["fallback_review"] and queued[0].context["peer_failed"] == "worker-4"


async def test_a_worker_whose_peer_review_failed_rests_before_the_next_one(env, scripted, monkeypatch):
    """Live 2026-11-26: worker-4's window could not send long prompts and failed three reviews in a row."""
    make, project_id = scripted
    page, runtime = make()
    job_id, _ = await _review_job(env, runtime, project_id)
    peer_module.PEER_COOLDOWN.clear()

    async def broken(self, db, job):
        job.status, job.error = "needs_human", "send_failed"
        await db.commit()
        return {"job_id": job.id, "status": job.status}

    monkeypatch.setattr(PeerReviewer, "_run_job", broken)
    async with env() as db:
        profile_id = (await db.get(Agent, "worker-4")).browser_profile_id
    await PeerReviewer("worker-4", profile_id, adapter=page, session_factory=env).run(job_id)
    try:
        assert "worker-4" in peer_module.PEER_COOLDOWN
        async with env() as db:
            assert "worker-4" not in {w.id for w in await PeerReviewPool()._free_workers(db)}
    finally:
        peer_module.PEER_COOLDOWN.clear()
