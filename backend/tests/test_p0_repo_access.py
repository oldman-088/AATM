"""P0 / E43: a worker whose Arena account cannot see the repository hands the task on instead of stopping."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_e_dashboard
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from test_e_dashboard import env  # noqa: F401 - fixture

from app.db.base import utcnow
from app.models import Agent, AgentMessage, Notification, Project, Task
from app.runtime.loop.common import RepoAccessError, _requeue_for_repo_access_impl, repo_access_denied

REPO = "acme/private-repo"


async def _setup(env, *, manual: bool = False):
    async with env() as db:
        project = Project(name="Private", slug="private", repo_url=f"https://github.com/{REPO}", default_branch="main")
        db.add(project)
        await db.flush()
        agent = await db.get(Agent, "worker-1")
        agent.browser_profile_id = agent.browser_profile_id or "profile-w1"
        task = Task(project_id=project.id, title="Add answer()", description="d", status="in_progress",
                    assigned_agent_id=agent.id, execution_mode="manual" if manual else "auto")
        db.add(task)
        await db.flush()
        message = AgentMessage(sender_id="manager-0", recipient_id=agent.id, task_id=task.id, instructions="go",
                               execution_status="running")
        db.add(message)
        agent.status, agent.current_task_id = "working", task.id
        await db.commit()
        return project.id, task.id, message.id


async def _denied_once(env, task_id, message_id):
    async with env() as db:
        task, message = await db.get(Task, task_id), await db.get(AgentMessage, message_id)
        agent = await db.get(Agent, "worker-1")
        await _requeue_for_repo_access_impl(db, message, task, agent,
                                            RepoAccessError(agent.browser_profile_id, REPO, f"Could not select {REPO}"))


async def _alerts(db) -> list[Notification]:
    return [n for n in (await db.execute(select(Notification))).scalars() if REPO in n.title]


async def test_task_goes_back_to_the_queue_and_the_owner_is_told_once(env):
    project_id, task_id, message_id = await _setup(env)
    await _denied_once(env, task_id, message_id)
    async with env() as db:
        task, agent, project = await db.get(Task, task_id), await db.get(Agent, "worker-1"), await db.get(Project, project_id)
        assert task.status == "queued" and task.assigned_agent_id is None
        assert agent.status == "idle" and agent.current_task_id is None
        assert "worker-1" in repo_access_denied(project)
        assert len(await _alerts(db)) == 1
    # The same worker failing again does not alert twice.
    async with env() as db:
        task = await db.get(Task, task_id)
        task.status, task.assigned_agent_id = "in_progress", "worker-1"
        (await db.get(AgentMessage, message_id)).execution_status = "running"
        await db.commit()
    await _denied_once(env, task_id, message_id)
    async with env() as db:
        assert len(await _alerts(db)) == 1


async def test_a_manual_task_is_blocked_with_the_reason(env):
    _project_id, task_id, message_id = await _setup(env, manual=True)
    await _denied_once(env, task_id, message_id)
    async with env() as db:
        task = await db.get(Task, task_id)
        assert task.status == "blocked" and REPO in task.error_message and "Grant the Arena GitHub app" in task.error_message


async def test_when_no_worker_can_see_the_repo_the_task_is_blocked(env):
    project_id, task_id, message_id = await _setup(env)
    async with env() as db:
        project = await db.get(Project, project_id)
        workers = (await db.execute(select(Agent).where(Agent.role == "worker", Agent.is_enabled.is_(True)))).scalars().all()
        for w in workers:
            w.browser_profile_id = w.browser_profile_id or f"profile-{w.id}"
        now = utcnow().isoformat()
        project.config = {"repo_access_denied": {w.id: {"at": now, "repo": REPO} for w in workers if w.id != "worker-1"}}
        await db.commit()
    await _denied_once(env, task_id, message_id)
    async with env() as db:
        assert (await db.get(Task, task_id)).status == "blocked"


def test_a_denial_expires_so_access_granted_later_is_tried_again():
    old = (utcnow() - timedelta(hours=3)).isoformat()
    fresh = utcnow().isoformat()
    project = Project(name="x", slug="x", config={"repo_access_denied": {"worker-1": {"at": old}, "worker-2": {"at": fresh},
                                                                         "worker-3": {"at": "garbage"}}})
    assert repo_access_denied(project) == {"worker-2"}
    assert repo_access_denied(None) == set()


async def test_the_scheduler_never_gives_the_project_to_a_denied_worker(env):
    from app.orchestration.scheduler import TaskScheduler

    async with env() as db:
        project = Project(name="Denied", slug="denied", repo_url=f"https://github.com/{REPO}", default_branch="main")
        db.add(project)
        await db.flush()
        workers = (await db.execute(select(Agent).where(Agent.role == "worker", Agent.is_enabled.is_(True)))).scalars().all()
        allowed = workers[-1].id
        now = utcnow().isoformat()
        project.config = {"repo_access_denied": {w.id: {"at": now} for w in workers if w.id != allowed}}
        for w in workers:
            w.status, w.current_task_id = "idle", None
        task = Task(project_id=project.id, title="T", description="d", status="queued")
        db.add(task)
        await db.commit()
        task_id = task.id
    async with env() as db:
        await TaskScheduler().run_orchestration_cycle(db, only_task_ids={task_id})
    async with env() as db:
        task = await db.get(Task, task_id)
        assert task.assigned_agent_id in (None, allowed)
