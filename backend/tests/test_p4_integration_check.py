"""P4 Q4: a project is closed only after one final integration check task has run the whole product."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_e_dashboard
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from test_e_dashboard import env  # noqa: F401 - fixture

from app.db.base import utcnow
from app.models import Project, Task
from app.orchestration.review import review_service


async def _project(env, **config) -> str:
    async with env() as db:
        project = Project(name=f"Int {sorted(config)}", slug=f"int-{len(config)}-{sorted(config)}"[:60], default_branch="main",
                          config=config)
        db.add(project)
        await db.flush()
        db.add_all([Task(project_id=project.id, title=f"Feature {i}", description="d", status="done",
                         created_at=utcnow() - timedelta(minutes=10)) for i in range(2)])
        await db.commit()
        return project.id


async def _check(env, project_id: str) -> bool:
    async with env() as db:
        return await review_service.check_project_completion(db, project_id)


async def _checks(env, project_id: str) -> list[Task]:
    async with env() as db:
        rows = (await db.execute(select(Task).where(Task.project_id == project_id))).scalars().all()
        return [t for t in rows if (t.idempotency_key or "").startswith("integration:")]


async def test_finished_work_gets_one_integration_task_before_the_project_closes(env):
    project_id = await _project(env)
    assert await _check(env, project_id) is False
    assert await _check(env, project_id) is False  # the queued check keeps the project open, and is not duplicated
    checks = await _checks(env, project_id)
    assert len(checks) == 1 and checks[0].status == "queued" and checks[0].priority == "high"
    assert "docs/INTEGRATION_REPORT.md" in checks[0].description and "authentication" in checks[0].description
    async with env() as db:
        (await db.get(Task, checks[0].id)).status = "done"
        await db.commit()
    assert await _check(env, project_id) is True
    async with env() as db:
        assert (await db.get(Project, project_id)).status == "completed"


async def test_work_added_after_the_check_gets_a_new_check(env):
    project_id = await _project(env)
    await _check(env, project_id)
    first = (await _checks(env, project_id))[0]
    async with env() as db:
        (await db.get(Task, first.id)).status = "done"
        db.add(Task(project_id=project_id, title="Late feature", description="d", status="done", created_at=utcnow() + timedelta(seconds=5)))
        await db.commit()
    assert await _check(env, project_id) is False
    assert len(await _checks(env, project_id)) == 2


async def test_the_check_can_be_turned_off_or_cancelled(env):
    off = await _project(env, integration_check=False)
    assert await _check(env, off) is True
    cancelled = await _project(env, note="cancel")
    await _check(env, cancelled)
    check = (await _checks(env, cancelled))[0]
    async with env() as db:
        (await db.get(Task, check.id)).status = "cancelled"
        await db.commit()
    assert await _check(env, cancelled) is True
