"""P3 O4/O5: project progress and remaining time from real task timings; the last-24-hours summary."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_e_dashboard
from __future__ import annotations

from datetime import timedelta

from httpx import ASGITransport, AsyncClient
from test_e_dashboard import _client_headers, env  # noqa: F401 - fixture

from app.db.base import utcnow
from app.main import app
from app.models import Project, Task


async def _get(env, path: str) -> dict:
    headers = await _client_headers(env)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(path, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_progress_and_eta_use_the_median_finished_task(env):
    now = utcnow()
    async with env() as db:
        project = Project(name="Prog", slug="prog", default_branch="main", config={"team": ["worker-1", "worker-2"]})
        db.add(project)
        await db.flush()
        for minutes in (10, 20, 90):
            db.add(Task(project_id=project.id, title=f"done {minutes}", description="d", status="merged", pr_number=1,
                        started_at=now - timedelta(minutes=minutes), completed_at=now))
        for i in range(3):
            db.add(Task(project_id=project.id, title=f"todo {i}", description="d", status="queued"))
        db.add(Task(project_id=project.id, title="dropped", description="d", status="cancelled"))
        await db.commit()
        pid = project.id
    progress = (await _get(env, f"/api/v1/projects/{pid}/timeline"))["progress"]
    assert (progress["total"], progress["done"], progress["remaining"], progress["percent"]) == (6, 3, 3, 50)
    assert progress["typical_task_minutes"] == 20.0 and progress["parallel_workers"] == 2
    assert progress["eta_minutes"] == 40  # ceil(3 / 2) rounds of a 20-minute task


async def test_eta_is_unknown_without_samples_and_zero_when_done(env):
    async with env() as db:
        fresh = Project(name="Fresh", slug="fresh", default_branch="main")
        finished = Project(name="Finished", slug="finished", default_branch="main")
        db.add_all([fresh, finished])
        await db.flush()
        db.add(Task(project_id=fresh.id, title="todo", description="d", status="queued"))
        db.add(Task(project_id=finished.id, title="done", description="d", status="done"))
        await db.commit()
        fresh_id, finished_id = fresh.id, finished.id
    assert (await _get(env, f"/api/v1/projects/{fresh_id}/timeline"))["progress"]["eta_minutes"] is None
    done = (await _get(env, f"/api/v1/projects/{finished_id}/timeline"))["progress"]
    assert done["percent"] == 100 and done["eta_minutes"] == 0


async def test_daily_summary_counts_the_last_day(env):
    async with env() as db:
        project = Project(name="Daily", slug="daily", default_branch="main")
        db.add(project)
        await db.flush()
        db.add(Task(project_id=project.id, title="Merged one", description="d", status="merged", pr_number=12))
        db.add(Task(project_id=project.id, title="Broken one", description="d", status="failed", error_message="boom"))
        await db.commit()
    data = await _get(env, "/api/v1/dashboard/daily")
    assert any(m["pr_number"] == 12 for m in data["merged_prs"])
    assert any(f["title"] == "Broken one" and f["error"] == "boom" for f in data["failed_or_blocked"])
    assert data["completed_tasks"] >= 1 and data["hours"] == 24
