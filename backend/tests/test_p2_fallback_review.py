"""P2 M5: a failed manager review is retried once as a short review; only then is a human asked."""

# ruff: noqa: F811 - pytest fixtures are re-exported
from __future__ import annotations

from sqlalchemy import select
from test_d_arena_connector import arena_settings, env  # noqa: F401 - fixtures
from test_r7_manager_arena import scripted  # noqa: F401 - fixture

from app.models import ArenaSession, ManagerJob, Task
from app.orchestration.manager_arena import FALLBACK_REVIEW_FILES, FALLBACK_REVIEW_MAX_CHARS


async def _review(env, runtime, project_id: str) -> tuple[str, str]:
    async with env() as db:
        parent = Task(project_id=project_id, title="Add login", status="pr_open", task_type="implementation",
                      assigned_agent_id="worker-1", pr_url="https://github.com/acme/demo/pull/7", pr_number=7)
        db.add(parent)
        await db.flush()
        review = Task(project_id=project_id, title="Review login", status="in_progress", task_type="review",
                      assigned_agent_id="manager-0", parent_task_id=parent.id)
        db.add(review)
        db.add(ArenaSession(kind="worker", agent_id="worker-1", profile_id="worker-1", task_id=parent.id, project_id=project_id,
                            phase="awaiting_approval", page_state="idle", report="## AATM REPORT\ndone", is_open=True))
        await db.commit()
        job = await runtime.enqueue(db, "review", project_id=project_id, review_task_id=review.id,
                                    context={"pr_number": 7, "parent_task_id": parent.id})
        return job.id, review.id


async def test_first_failure_queues_one_short_review_and_the_second_asks_a_human(env, scripted):
    make, project_id = scripted
    _page, runtime = make()
    job_id, review_id = await _review(env, runtime, project_id)
    async with env() as db:
        await runtime._needs_human(db, await db.get(ManagerJob, job_id), "review had no verdict", raw="prose")
    async with env() as db:
        jobs = list((await db.execute(select(ManagerJob).where(ManagerJob.review_task_id == review_id)
                                      .order_by(ManagerJob.created_at))).scalars())
        assert [j.status for j in jobs] == ["failed", "queued"]
        retry = jobs[1]
        assert retry.context["fallback_review"] is True and retry.context["fallback_of"] == job_id
        assert retry.context["pr_number"] == 7
        assert (await db.get(Task, review_id)).status == "in_progress"
        await runtime._needs_human(db, retry, "still no verdict", raw="prose")
    async with env() as db:
        jobs = list((await db.execute(select(ManagerJob).where(ManagerJob.review_task_id == review_id))).scalars())
        assert sorted(j.status for j in jobs) == ["failed", "needs_human"]
        assert (await db.get(Task, review_id)).status == "blocked"


async def test_the_short_review_is_one_small_part(env, scripted, monkeypatch):
    from app.orchestration import arena_runner as arena_runner_module

    make, project_id = scripted
    _page, runtime = make()
    files = [{"filename": f"app/m{i}.py", "status": "modified", "additions": 30, "deletions": 1,
              "patch": "+x = 1\n" * 30} for i in range(60)]

    async def material(db, parent):
        return {"changed_files": files, "mechanical_discrepancies": []}

    async def compare(project, session):
        return {"ok": True, "files": files}

    monkeypatch.setattr(arena_runner_module.arena_runner, "review_material", material)
    monkeypatch.setattr(arena_runner_module.arena_runner, "_compare", compare)
    job_id, _ = await _review(env, runtime, project_id)
    async with env() as db:
        job = await db.get(ManagerJob, job_id)
        full = await runtime._review_prompts(db, job, None, [])
        job.context = {**job.context, "fallback_review": True}
        short = await runtime._review_prompts(db, job, None, [])
    assert len(full) == 2 and len(short) == 1
    assert len(short[0]) <= FALLBACK_REVIEW_MAX_CHARS
    assert job.context["changed_files"] == [f"app/m{i}.py" for i in range(FALLBACK_REVIEW_FILES)]



def test_worker_and_review_prompts_require_auth_on_new_routes():
    from app.orchestration.arena_prompts import build_manager_review_prompt, build_task_prompt

    task = Task(id="t-q5", project_id="p", title="Add /orders", description="Add GET /orders.", status="queued")
    worker = build_task_prompt(task, None, expected_head_sha="a" * 40)
    review = build_manager_review_prompt(task, acceptance=["works"], report="done", diff="+x", pr_url=None, round_number=1,
                                         project=None, team=[])
    assert "every new API route requires authentication" in worker and "401 or 403" in worker
    assert "a new API route without authentication" in review
