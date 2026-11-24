"""P2 T16 (E31, E41 c): tasks that change the same files, or two migrations, never run at the same time."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_e_dashboard
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select
from test_e_dashboard import env  # noqa: F401 - fixture

from app.models import Agent, Project, Task
from app.orchestration.arena_prompts import build_manager_plan_prompt
from app.orchestration.scheduler import TaskScheduler, footprint_conflict, task_footprint


def _t(id_: str, description: str, status: str = "in_progress", project: str = "p1", title: str = "T"):
    return SimpleNamespace(id=id_, project_id=project, status=status, title=title, description=description)


def test_footprint_reads_files_and_migrations_but_ignores_shared_docs():
    paths, migration = task_footprint(_t("a", "Change `backend/app/api/users.py` and app.css; contract in docs/API.md and README.md"))
    assert paths == {"backend/app/api/users.py", "app.css"} and migration is False
    assert task_footprint(_t("b", "Add an Alembic revision for the phones table"))[1] is True
    assert task_footprint(_t("c", "Edit backend/migrations/versions/0003_x.py"))[1] is True


def test_conflicts_are_same_file_while_running_or_two_migrations_even_unmerged():
    task = _t("new", "Edit backend/app/api/users.py", status="queued")
    assert "users.py" in footprint_conflict(task, [_t("run", "Refactor backend/app/api/users.py")])
    assert footprint_conflict(task, [_t("run", "Edit backend/app/api/users.py", project="other")]) is None
    assert footprint_conflict(task, [_t("pr", "Edit backend/app/api/users.py", status="in_review")]) is None
    assert footprint_conflict(task, [_t("run", "Edit frontend/src/App.tsx; contract docs/API.md")]) is None
    mig = _t("m2", "Add an alembic migration for orders", status="queued")
    assert "migrations" in footprint_conflict(mig, [_t("m1", "New Alembic revision for users", status="pr_open")])


def test_plan_prompt_gives_migrations_to_one_task():
    assert "database migrations" in build_manager_plan_prompt("Build it", None, [], [])


async def test_scheduler_holds_back_a_task_that_shares_a_file_with_a_running_one(env):
    async with env() as db:
        project = Project(name="Files", slug="files", default_branch="main")
        db.add(project)
        await db.flush()
        workers = (await db.execute(select(Agent).where(Agent.role == "worker", Agent.is_enabled.is_(True)))).scalars().all()
        for w in workers:
            w.status, w.current_task_id = "idle", None
        running = Task(project_id=project.id, title="Users API", description="Change backend/app/api/users.py",
                       status="in_progress", assigned_agent_id=workers[0].id)
        workers[0].status, workers[0].current_task_id = "working", None
        same = Task(project_id=project.id, title="Users fields", description="Add a field in backend/app/api/users.py", status="queued")
        other = Task(project_id=project.id, title="Orders", description="Create backend/app/api/orders.py", status="queued")
        db.add_all([running, same, other])
        await db.commit()
        ids = {same.id, other.id}
        same_id, other_id = same.id, other.id
    async with env() as db:
        await TaskScheduler().run_orchestration_cycle(db, only_task_ids=ids)
    async with env() as db:
        assert (await db.get(Task, same_id)).status == "queued"
        assert (await db.get(Task, other_id)).status != "queued"
