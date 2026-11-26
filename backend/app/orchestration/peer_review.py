"""Peer review: an idle worker reviews a pull request in parallel with the manager (owner, 2026-11-26).

On 2026-11-26 six PRs waited for the manager, who reviews one at a time (10-20 minutes each), while the
workers sat idle. Now an idle worker whose window is free takes the oldest queued review job and runs it
in its own Arena window with the same prompt, parser and verdict path as the manager:

* a worker never reviews its own pull request;
* only idle, enabled, connected workers that no Arena session reserves take a review, and each review
  runs in a fresh chat that is closed when it ends, so the worker's own PR chats stay untouched;
* a shared claim set keeps the manager and the peers from running the same job twice;
* the verdict goes through ``review_service.submit_review`` exactly like the manager's, so a merge still
  needs a green CI on the PR's head commit (merge queue).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.arena_sessions import append_event, can_park, close_session, open_session_for, park_session
from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.models import Agent, ArenaSession, AuditLog, ChatMessage, ManagerJob, Task
from app.orchestration.manager_arena import CLAIMED_JOBS, ManagerArenaRuntime

logger = logging.getLogger(__name__)

PEER_SESSION_KIND = "peer_review"


class PeerReviewer(ManagerArenaRuntime):
    """The manager's review flow, run in one worker's Arena window."""

    def __init__(self, agent_id: str, profile_id: str, adapter: Any = None, session_factory: Any = None) -> None:
        super().__init__(adapter=adapter, session_factory=session_factory)
        self.agent_id = agent_id
        self.profile_id = profile_id

    async def _profile_id(self, db: AsyncSession) -> str:
        return self.profile_id

    async def _set_manager_status(self, db: AsyncSession, status: str) -> None:
        return None  # the worker's own status is set by the pool, not by each step of the job

    async def _reply(self, db: AsyncSession, job: ManagerJob, content: str, **metadata: Any) -> ChatMessage | None:
        return None  # progress notes belong to the manager's chat; the verdict itself is posted on the PR

    async def _open_session(self, db: AsyncSession) -> ArenaSession | None:
        return (await db.execute(
            select(ArenaSession).where(ArenaSession.kind == PEER_SESSION_KIND, ArenaSession.profile_id == self.profile_id,
                                       ArenaSession.is_open.is_(True))
            .order_by(ArenaSession.created_at.desc())
        )).scalars().first()

    async def _ensure_session(self, db: AsyncSession, job: ManagerJob, profile_id: str) -> ArenaSession:
        """Every review starts in a new chat: no team context is needed to review one PR."""
        session = await self._open_session(db)
        if session is not None:
            return session
        await self.adapter.new_session(profile_id)
        session = ArenaSession(kind=PEER_SESSION_KIND, agent_id=self.agent_id, profile_id=profile_id,
                               project_id=job.project_id, phase="manager_ready", page_state="ready", events=[])
        append_event(session, "created", f"peer review job {job.id}")
        db.add(session)
        await db.commit()
        return session

    async def _apply_review(self, db: AsyncSession, job: ManagerJob, verdict: dict[str, Any], *, raw: str) -> None:
        from app.orchestration.review import review_service
        from app.orchestration.scheduler import task_scheduler

        review = await db.get(Task, job.review_task_id) if job.review_task_id else None
        reviewer = await db.get(Agent, self.agent_id)
        if review is None or reviewer is None:
            await self._finish(db, job, status="failed", error="Review task missing.", raw=raw)
            return
        body = "\n".join([f"Peer review by {reviewer.name or reviewer.id}:", verdict["summary_ar"],
                          *[f"- {c}" for c in verdict["comments"]]])
        event = "REQUEST_CHANGES" if verdict["verdict"] == "request_changes" else "COMMENT"
        outcome = await review_service.submit_review(db, review, reviewer, event=event, body=body)
        review = await db.get(Task, job.review_task_id)
        if review is not None and review.status not in {"completed", "done"}:
            await task_scheduler.complete_task(db, review, summary=f"Peer verdict ({self.agent_id}): {verdict['verdict']}",
                                               target_status="completed")
        await self._finish(db, job, result={"verdict": verdict, "reviewer": self.agent_id,
                                            "outcome": {k: v for k, v in outcome.items() if k != "github"}}, raw=raw)
        await event_bus.emit("manager.review", {"review_task_id": job.review_task_id, "verdict": verdict["verdict"],
                                                "reviewer": self.agent_id,
                                                "follow_up_task_id": outcome.get("follow_up_task_id"),
                                                "merge_approval_id": outcome.get("merge_approval_id")})

    async def run(self, job_id: str) -> dict[str, Any]:
        """Run one claimed review job, then free the worker and close the review chat."""
        try:
            async with self.session_factory() as db:
                job = await db.get(ManagerJob, job_id)
                if job is None or job.status != "queued":
                    return {"job_id": job_id, "status": "skipped"}
                job.context = {**(job.context or {}), "reviewer": self.agent_id}
                await db.commit()
                result = await self._run_job(db, job)
                job = await db.get(ManagerJob, job_id, populate_existing=True)
                if job is not None and job.status in {"failed", "needs_human", "waiting_connection"}:
                    # No verdict from the peer: the manager reviews it instead of the owner being asked.
                    job.status, job.attempts, job.error = "queued", 0, None
                    job.context = {**(job.context or {}), "peer_failed": self.agent_id}
                    await db.commit()
                    result = {**result, "status": "requeued_for_manager"}
                return result
        except Exception as exc:  # noqa: BLE001 - a broken peer review goes back to the queue
            logger.warning("peer review %s by %s failed: %s", job_id, self.agent_id, type(exc).__name__, exc_info=True)
            async with self.session_factory() as db:
                job = await db.get(ManagerJob, job_id)
                if job is not None and job.status == "running":
                    job.status = "queued"
                    job.context = {**(job.context or {}), "peer_failed": self.agent_id}
                    await db.commit()
            return {"job_id": job_id, "status": "requeued", "error": type(exc).__name__}
        finally:
            CLAIMED_JOBS.discard(job_id)
            async with self.session_factory() as db:
                session = await self._open_session(db)
                if session is not None:
                    await close_session(db, session, phase="closed", reason="peer review finished")
                job = await db.get(ManagerJob, job_id)
                review = await db.get(Task, job.review_task_id) if job and job.review_task_id else None
                if review is not None and review.lock_owner == self.agent_id:
                    review.lock_owner = None  # if no verdict was given, its job is queued again for the manager
                worker = await db.get(Agent, self.agent_id)
                if worker is not None and worker.status == "working" and worker.current_task_id in {None, getattr(review, "id", None)}:
                    worker.status, worker.current_task_id = "idle", None
                await db.commit()


class PeerReviewPool:
    """Hands queued review jobs to idle workers, at most ``PEER_REVIEW_MAX`` at once."""

    def __init__(self) -> None:
        self.running: dict[str, asyncio.Task] = {}  # worker id -> its review

    async def tick(self, db: AsyncSession, *, adapter: Any = None) -> dict[str, Any]:
        settings = get_settings()
        self.running = {w: t for w, t in self.running.items() if not t.done()}
        if not settings.PEER_REVIEW_ENABLED:
            return {"enabled": False}
        free = max(0, settings.PEER_REVIEW_MAX - len(self.running))
        if not free:
            return {"running": sorted(self.running)}
        jobs = [j for j in (await db.execute(
            select(ManagerJob).where(ManagerJob.kind == "review", ManagerJob.status == "queued")
            .order_by(ManagerJob.created_at.asc())
        )).scalars() if j.id not in CLAIMED_JOBS and (j.context or {}).get("peer_failed") is None]
        if not jobs:
            return {"running": sorted(self.running)}
        workers = await self._free_workers(db)
        started: list[dict[str, str]] = []
        for job in jobs:
            if not free or not workers:
                break
            review = await db.get(Task, job.review_task_id) if job.review_task_id else None
            parent = await db.get(Task, review.parent_task_id) if review and review.parent_task_id else None
            author = parent.assigned_agent_id if parent else None
            worker = next((w for w in workers if w.id != author), None)
            if worker is None or review is None:
                continue
            workers.remove(worker)
            own = await open_session_for(db, worker.browser_profile_id)
            if own is not None:
                if not can_park(own):
                    continue  # its window drives its own work: never take it for a review
                # Its PR chat is kept by URL; a correction brings the window back to it (parked sessions).
                park_session(own, f"peer review of job {job.id}")
            CLAIMED_JOBS.add(job.id)  # no await since the check above: the manager cannot take it in between
            worker.status, worker.current_task_id = "working", review.id
            review.lock_owner = worker.id
            db.add(AuditLog(actor=worker.id, action="peer_review.started", resource_type="manager_job", resource_id=job.id,
                            details={"review_task_id": review.id, "pr_number": (job.context or {}).get("pr_number"),
                                     "author": author, "at": utcnow().isoformat()}))
            await db.commit()
            reviewer = PeerReviewer(worker.id, worker.browser_profile_id, adapter=adapter)
            self.running[worker.id] = asyncio.create_task(reviewer.run(job.id))
            started.append({"worker": worker.id, "job": job.id})
            free -= 1
        return {"started": started, "running": sorted(self.running)}

    async def _free_workers(self, db: AsyncSession) -> list[Agent]:
        from app.orchestration.arena_runner import arena_reservations

        reserved = await arena_reservations(db)
        queued_for = {t.assigned_agent_id for t in (await db.execute(select(Task).where(Task.status == "queued"))).scalars()
                      if t.task_type != "review"}
        candidates = list((await db.execute(select(Agent).where(
            Agent.role == "worker", Agent.is_enabled.is_(True), Agent.is_emergency_stopped.is_(False),
            Agent.status == "idle", Agent.browser_profile_id.is_not(None),
        ).order_by(Agent.id))).scalars())
        # A worker with queued work of its own keeps its window for that work (it may be waiting a moment
        # for a file-overlap guard), unless every candidate has some: then reviewing beats idling.
        free = [w for w in candidates if w.id not in reserved and w.id not in self.running]
        without_work = [w for w in free if w.id not in queued_for]
        return without_work or free


peer_review_pool = PeerReviewPool()
