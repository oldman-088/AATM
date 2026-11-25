"""E55: reviews of already-merged PRs are closed with their waiting jobs; a review whose job failed is shown as blocked."""

# ruff: noqa: F811 - pytest fixtures are re-exported
from __future__ import annotations

from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures
from test_r7_manager_arena import scripted  # noqa: F401 - fixture

from app.models import ManagerJob, Task


async def _review(env, project_id: str, parent_status: str, review_status: str, job_status: str | None) -> tuple[str, str | None]:
    async with env() as db:
        parent = Task(project_id=project_id, title="Feature", status=parent_status, task_type="implementation", pr_number=5)
        db.add(parent)
        await db.flush()
        review = Task(project_id=project_id, title="Review PR #5", status=review_status, task_type="review",
                      assigned_agent_id="manager-0", parent_task_id=parent.id)
        db.add(review)
        await db.flush()
        job_id = None
        if job_status:
            job = ManagerJob(kind="review", project_id=project_id, review_task_id=review.id, status=job_status, error="no JSON")
            db.add(job)
            await db.flush()
            job_id = job.id
        await db.commit()
        return review.id, job_id


async def test_stale_reviews_are_settled(env, scripted):
    make, project_id = scripted
    _page, runtime = make()
    merged_blocked, merged_job = await _review(env, project_id, "merged", "blocked", "needs_human")
    merged_queued, failed_job = await _review(env, project_id, "merged", "queued", "failed")
    open_failed, _ = await _review(env, project_id, "in_review", "queued", "failed")
    open_waiting, waiting_job = await _review(env, project_id, "in_review", "blocked", "needs_human")
    async with env() as db:
        assert await runtime._settle_stale_reviews(db) == 3
    async with env() as db:
        assert (await db.get(Task, merged_blocked)).status == "cancelled"
        assert (await db.get(ManagerJob, merged_job)).status == "failed"
        assert (await db.get(Task, merged_queued)).status == "cancelled"
        assert (await db.get(ManagerJob, failed_job)).status == "failed"
        blocked = await db.get(Task, open_failed)
        assert blocked.status == "blocked" and "no JSON" in blocked.error_message
        assert (await db.get(Task, open_waiting)).status == "blocked"  # a live PR keeps its human question
        assert (await db.get(ManagerJob, waiting_job)).status == "needs_human"
    async with env() as db:
        assert await runtime._settle_stale_reviews(db) == 0
