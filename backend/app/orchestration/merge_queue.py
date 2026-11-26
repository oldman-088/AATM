"""P2 safe auto-merge queue (AATM development plan M1–M6).

For a project whose ``config["auto_merge"]`` is true, AATM merges a pull request itself, one at a time, only when:

- the manager's review approved it (the task is ``in_review`` / ``reviewed`` with a pending, non-escalated merge
  approval) (M2),
- CI on the PR's head commit is green and GitHub reports no conflict (M2),
- a PR that is behind its base is first updated from it, so CI runs on what will be merged (E41 a),
- the base branch is still green after the previous merge (M3): a red base pauses the queue and opens one
  repair task; a later green run resumes it.

A conflicting PR goes back to its worker as an "update the branch and resolve the conflict" follow-up (M4).
Projects without the setting are untouched: their merges keep waiting for the owner's approval (M6).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.git_workflows.manager import git_workflow_manager
from app.models import ApprovalRequest, AuditLog, Notification, Project, Task
from app.models.project import project_base_branch

DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt", ".adoc")
NO_BASE_CI_AFTER_SECONDS = 600.0  # a base with no check run this long after a merge has no CI at all


def docs_only(files: list[str] | None) -> bool:
    """Every changed file is documentation (Markdown, reStructuredText, plain text)."""
    return bool(files) and all(f.lower().endswith(DOC_SUFFIXES) for f in files)


def _base_has_no_ci(state: dict[str, Any]) -> bool:
    """The base reported no checks at all well after the last merge: the repository has no CI, so do not wait forever."""
    at = (state.get("last_merge") or {}).get("at")
    if not at:
        return True
    try:
        merged = datetime.fromisoformat(at)
    except ValueError:
        return True
    if merged.tzinfo is None:
        merged = merged.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - merged).total_seconds() >= NO_BASE_CI_AFTER_SECONDS

logger = logging.getLogger(__name__)

ACTOR = "aatm-merge-queue"
QUEUE_KEY = "merge_queue"
PROJECT_INTERVAL_SECONDS = 60.0  # GitHub is read at most once a minute per project


def auto_merge_enabled(project: Project | None) -> bool:
    return bool(project is not None and (project.config or {}).get("auto_merge") is True)


def _state(project: Project) -> dict[str, Any]:
    return dict((project.config or {}).get(QUEUE_KEY) or {})


def _save(project: Project, state: dict[str, Any]) -> None:
    project.config = {**(project.config or {}), QUEUE_KEY: state}


class MergeQueue:
    def __init__(self, github: Any = None, *, interval: float = PROJECT_INTERVAL_SECONDS) -> None:
        if github is None:
            from app.github_client.client import github_client

            github = github_client
        self.github = github
        self.interval = interval
        self._last_tick: dict[str, float] = {}

    async def tick(self, db: AsyncSession) -> dict[str, Any]:
        """One pass over every auto-merge project. Never raises: a failure is logged and the next pass retries."""
        results: dict[str, Any] = {}
        projects = (await db.execute(select(Project).where(Project.status == "active"))).scalars().all()
        for project in projects:
            if not auto_merge_enabled(project):
                continue
            now = time.monotonic()
            if now - self._last_tick.get(project.id, -1e9) < self.interval:
                continue
            self._last_tick[project.id] = now
            try:
                results[project.id] = await self.project_tick(db, project)
            except Exception as exc:  # noqa: BLE001 - the queue must never break dispatch
                logger.warning("Merge queue failed for project %s: %s", project.id, type(exc).__name__)
                await db.rollback()
                results[project.id] = {"error": type(exc).__name__}
        return results

    async def project_tick(self, db: AsyncSession, project: Project) -> dict[str, Any]:
        repo = git_workflow_manager.repo_slug_from_url(project.repo_url)
        if not repo:
            return {"status": "no_repo"}
        base = project_base_branch(project)
        state = _state(project)

        # M3: after a merge (or while paused for a red base), the base branch must be green before anything else.
        if state.get("awaiting_base") or state.get("paused"):
            base_ci = await self.github.branch_ci(repo, base)
            if not base_ci.get("available") or base_ci.get("ci") == "pending":
                return {"status": "waiting_base", "ci": base_ci.get("ci")}
            if base_ci.get("ci") == "none" and not _base_has_no_ci(state):
                return {"status": "waiting_base", "ci": "none"}
            if base_ci["ci"] == "failure":
                if not state.get("paused"):
                    await self._pause(db, project, state, base, repo, base_ci)
                return {"status": "paused", "base_sha": base_ci.get("head_sha")}
            if state.get("paused"):
                self._audit(db, project, "merge_queue.resumed", {"base_sha": base_ci.get("head_sha")})
            state.update({"awaiting_base": False, "paused": None})
            _save(project, state)
            await db.commit()

        task, approval = await self._next_candidate(db, project)
        if task is None or approval is None:
            return {"status": "idle"}
        pr = await self.github.pull_request_state(repo, int(task.pr_number))
        if not pr.get("available") or pr.get("state") != "OPEN":
            return {"status": "pr_not_open", "task_id": task.id, "state": pr.get("state")}
        if pr.get("base") and pr["base"] != base:
            return {"status": "wrong_base", "task_id": task.id, "base": pr["base"]}
        head = pr.get("head_sha") or ""
        if pr.get("conflicting"):
            created = await self.conflict_follow_up(db, task, base, head)
            return {"status": "conflict", "task_id": task.id, "follow_up_created": created}
        if pr.get("merge_state") == "BEHIND":
            # E41 (a): bring the PR up to date with the base first, so CI tests what will really be merged.
            key = f"update_branch:{task.id}:{head}"
            if key not in (state.get("updated") or []):
                result = await self.github.update_pull_request_branch(repo, int(task.pr_number), head)
                state["updated"] = ((state.get("updated") or []) + [key])[-50:]
                _save(project, state)
                self._audit(db, project, "merge_queue.update_branch", {"task_id": task.id, "pr_number": task.pr_number,
                                                                       "head_sha": head, "ok": result.get("ok")})
                await db.commit()
                return {"status": "updating_branch", "task_id": task.id, "ok": result.get("ok")}
            return {"status": "waiting_update", "task_id": task.id}
        if pr.get("ci") == "failure":
            await self._notify_once(db, project, state, f"ci_failed:{task.id}:{head}",
                                    f"CI failed on PR #{task.pr_number}",
                                    f"{task.title}: " + ", ".join(c["name"] for c in pr.get("checks", []) if c.get("result") == "failure"))
            return {"status": "ci_failed", "task_id": task.id}
        if pr.get("ci") == "none" and docs_only(pr.get("files")) and get_settings().AUTO_MERGE_DOCS_WITHOUT_CI:
            # Owner 2026-11-26: an approved PR that changes documentation only may merge without CI (the repository
            # may have no workflows yet). Before this the owner had to merge it by hand and its worker stayed reserved.
            return await self._merge(db, project, state, task, approval, head, reason="documentation only, no CI checks")
        if pr.get("ci") == "none":
            await self._notify_once(db, project, state, f"ci_none:{task.id}:{head}",
                                    f"PR #{task.pr_number} has no CI checks",
                                    "Auto-merge needs green CI on the head commit; merge it yourself or add CI.")
            return {"status": "no_ci", "task_id": task.id}
        if pr.get("ci") != "success":
            return {"status": "waiting_ci", "task_id": task.id}
        return await self._merge(db, project, state, task, approval, head)

    async def _next_candidate(self, db: AsyncSession, project: Project) -> tuple[Task | None, ApprovalRequest | None]:
        """The oldest approved PR task of this project with a pending, non-escalated merge approval (M1: in order)."""
        tasks = (await db.execute(
            select(Task).where(Task.project_id == project.id, Task.task_type == "implementation", Task.status == "in_review",
                               Task.review_status == "reviewed", Task.pr_number.is_not(None))
            .order_by(Task.updated_at.asc())
        )).scalars().all()
        if not tasks:
            return None, None
        approvals = (await db.execute(select(ApprovalRequest).where(
            ApprovalRequest.action_type == "merge", ApprovalRequest.status == "pending"))).scalars().all()
        by_task = {(a.payload or {}).get("task_id"): a for a in approvals if not (a.payload or {}).get("escalated")}
        for task in tasks:
            if task.id in by_task:
                return task, by_task[task.id]
        return None, None

    async def _merge(self, db: AsyncSession, project: Project, state: dict[str, Any], task: Task,
                     approval: ApprovalRequest, head: str, *, reason: str = "") -> dict[str, Any]:
        from app.orchestration.review import review_service

        approval.status = "approved"
        approval.reviewed_by = ACTOR
        approval.review_note = (f"Auto-merge: manager approved, {reason}, no conflict." if reason
                                else f"Auto-merge: manager approved, CI green on {head[:12]}, no conflict.")
        approval.decided_at = utcnow()
        self._audit(db, project, "merge_queue.approved", {"task_id": task.id, "pr_number": task.pr_number, "head_sha": head,
                                                          "approval_id": approval.id})
        await db.commit()
        result = await review_service.merge_task(db, task, approval, actor=ACTOR)
        if not result.get("ok"):
            # Leave the approval approved but unused; the owner sees the error and can merge from the PR card.
            await self._notify_once(db, project, state, f"merge_failed:{task.id}:{head}", f"Auto-merge of PR #{task.pr_number} failed",
                                    str(result.get("error") or result.get("code") or "merge failed")[:300])
            return {"status": "merge_failed", "task_id": task.id, "error": result.get("error")}
        state.update({"awaiting_base": True, "last_merge": {"task_id": task.id, "pr_number": task.pr_number, "head_sha": head,
                                                             "at": utcnow().isoformat()}})
        _save(project, state)
        await db.commit()
        await event_bus.emit("merge_queue.merged", {"project_id": project.id, "task_id": task.id, "pr_number": task.pr_number})
        return {"status": "merged", "task_id": task.id, "pr_number": task.pr_number}

    async def _pause(self, db: AsyncSession, project: Project, state: dict[str, Any], base: str, repo: str,
                     base_ci: dict[str, Any]) -> None:
        last = state.get("last_merge") or {}
        failing = ", ".join(c["name"] for c in base_ci.get("checks", []) if c.get("result") == "failure") or "CI"
        title = f"Repair {base}: CI is red after merging PR #{last.get('pr_number', '?')}"[:240]
        sha = base_ci.get("head_sha") or ""
        key = f"repair_base:{project.id}:{sha[:40]}"[:128]
        if (await db.execute(select(Task).where(Task.idempotency_key == key))).scalars().first() is None:
            db.add(Task(project_id=project.id, title=title, priority="high", status="queued", task_type="implementation",
                        idempotency_key=key,
                        description=(f"CI on {base} ({sha[:12]}) in {repo} is red after an automatic merge. Failing: {failing}. "
                                     f"Find the cause, fix it on a branch from the latest {base}, keep every test passing, and "
                                     "open a pull request. The merge queue stays paused until CI on the base is green.")))
        state.update({"paused": f"{base} red at {sha[:12]}", "awaiting_base": True})
        _save(project, state)
        db.add(Notification(category="review", severity="critical", title=f"Merge queue paused: {base} is red",
                            message=f"{project.name}: failing {failing}. A repair task was created.", link="/tasks"))
        self._audit(db, project, "merge_queue.paused", {"base_sha": sha, "failing": failing, "last_merge": last})
        await db.commit()

    async def conflict_follow_up(self, db: AsyncSession, task: Task, base: str, head: str, *, actor: str = ACTOR) -> bool:
        """M4: the worker updates its branch from the base and resolves the conflict, once per PR head.

        Used by the queue and by the PR card's "Resolve conflict" button (``actor`` is then the owner)."""
        key = f"conflict:{task.id}:{head[:40]}"[:128]
        if (await db.execute(select(Task).where(Task.idempotency_key == key))).scalars().first() is not None:
            return False
        db.add(Task(
            project_id=task.project_id, division_code=task.division_code, priority=task.priority,
            title=f"Resolve the merge conflict on PR #{task.pr_number}: {task.title}"[:240],
            description=(f"Your pull request #{task.pr_number} conflicts with {base}. Bring your branch up to date with "
                         f"origin/{base}, resolve every conflict keeping the intent of both sides, run the tests, and push "
                         "to the same branch. Do not open a new pull request."),
            execution_mode="manual", status="queued", assigned_agent_id=task.assigned_agent_id, task_type="follow_up",
            parent_task_id=task.id, review_round=task.review_round, idempotency_key=key, worktree_path=task.worktree_path,
            branch_name=task.branch_name, pr_number=task.pr_number, pr_url=task.pr_url,
        ))
        db.add(AuditLog(actor=actor, action="merge_queue.conflict_follow_up", resource_type="project", resource_id=task.project_id,
                        details={"task_id": task.id, "pr_number": task.pr_number, "head_sha": head}))
        await db.commit()
        return True

    async def _notify_once(self, db: AsyncSession, project: Project, state: dict[str, Any], key: str, title: str,
                           message: str) -> None:
        sent = list(state.get("notified") or [])
        if key in sent:
            return
        state["notified"] = (sent + [key])[-50:]
        _save(project, state)
        db.add(Notification(category="review", severity="warning", title=title[:200], message=message[:1_000], link="/git"))
        await db.commit()

    @staticmethod
    def _audit(db: AsyncSession, project: Project | None, action: str, details: dict[str, Any], *,
               project_id: str | None = None) -> None:
        db.add(AuditLog(actor=ACTOR, action=action, resource_type="project",
                        resource_id=(project.id if project else project_id), details=details))


merge_queue = MergeQueue()
