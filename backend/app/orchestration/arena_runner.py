"""Arena Agent orchestration: one Arena Agent session per employee, end to end.

Workflow for a task assigned to an employee in Arena Agent mode::

    new_session -> submit task prompt -> monitor -> Keep working -> ... -> idle
      -> GitHub API: PR "[AATM-<task_id>] ..." on an arena/* branch -> associate (pr_open)
      -> read_last_reply = agent report -> reviewer verifies report against the real diff
      -> corrections go to the SAME session as a follow-up message -> new report -> review again
      -> owner approval in the Approval Center -> squash merge -> session closed
      -> the employee's next task starts with new_session (Arena may lock a merged branch).

``run_message`` is called by the AgentLoop when it claims such a task (it starts or resumes the
session and submits); ``tick`` runs every orchestration cycle and advances every open session. All
state lives in ``arena_sessions`` / ``tasks``, so a backend or bridge restart resumes monitoring.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.arena_sessions import (
    ArenaSessionConflict,
    append_event,
    apply_bridge_state,
    claim_session,
    close_session,
    session_for_task,
)
from app.core.events import event_bus
from app.db.base import utcnow
from app.git_workflows.manager import git_workflow_manager
from app.models import (
    Agent,
    AgentMessage,
    ArenaSession,
    AuditLog,
    BrowserProfile,
    GitWorkflowRecord,
    Notification,
    Project,
    Task,
)
from app.models.project import configured_base_branch, project_base_branch
from app.orchestration.arena_prompts import (
    base_branch_publish_steps,
    build_correction_prompt,
    build_report_request,
    build_task_prompt,
    compare_report_to_changes,
    pr_title_prefix,
    reply_has_final_report,
    repo_slug,
    report_is_complete,
    reports_github_blocked,
)
from app.providers.adapters.arena_agent import ArenaAgentAdapter, ArenaAgentError, keep_working_allowed
from app.runtime.agent_protocol import redact_secrets
from app.runtime.temporal import to_naive_utc

logger = logging.getLogger(__name__)

MONITORED_PHASES = {"working", "correcting", "reporting"}
RESERVING_PHASES = MONITORED_PHASES | {"reviewing", "awaiting_approval", "starting", "submitting", "needs_human"}
STALL_MINUTES = 20
NO_PR_NUDGES = 2          # follow-ups asking for the PR before the task is escalated to the owner
NO_PR_QUIET_CHECKS = 2    # quiet GitHub checks without a PR before a nudge
SUBMIT_FAILURES_BEFORE_HUMAN = 3
MAX_REPORT_CHARS = 60_000
LIVE_CODE_MIN_SECONDS = 10.0   # at most one GitHub live-diff publish per session per interval
LIVE_CODE_MAX_FILES = 20       # files streamed per new head (the summary lists all of them)


def _now() -> datetime:
    return utcnow()


def _naive(dt: datetime | None) -> datetime | None:
    return to_naive_utc(dt) if dt is not None else None


class ArenaRunnerError(RuntimeError):
    pass


async def arena_reservations(db: AsyncSession) -> dict[str, str]:
    """agent_id -> implementation task id for employees whose Arena session is still in use.

    The employee stays bound to that session (so corrections reach the same Arena session) until
    the PR is merged or the task is closed; only that task's follow-ups are dispatched to them.
    """
    rows = (await db.execute(
        select(ArenaSession).where(ArenaSession.is_open.is_(True), ArenaSession.phase.in_(RESERVING_PHASES))
    )).scalars()
    return {s.agent_id: s.task_id for s in rows if s.task_id}


def live_diff_summary(session: ArenaSession, parent: Task, changes: dict[str, Any]) -> dict[str, Any]:
    from app.runtime.code_stream import is_streamable_path

    files = [
        {"filename": f["filename"], "status": f["status"], "additions": f["additions"], "deletions": f["deletions"]}
        for f in changes.get("files", []) if is_streamable_path(f["filename"])
    ]
    return {
        "session_id": session.id,
        "agent_id": session.agent_id,
        "task_id": parent.id,
        "branch": session.branch_name,
        "pr_url": session.pr_url,
        "head_sha": changes.get("head_sha") or session.head_sha,
        "compare_url": changes.get("html_url"),
        "ahead_by": changes.get("ahead_by", 0),
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
        "commits": [{"sha": c["sha"][:12], "message": redact_secrets(c["message"])[:200]} for c in changes.get("commits", [])][-20:],
        "files": files,
        "hidden_files": len(changes.get("files", [])) - len(files),
        "at": _now().isoformat(),
    }


# The chat is not on the task's repository (or that cannot be proven): retrying could write elsewhere.
REPO_SAFETY_CODES = {"repo_unknown", "repo_mismatch", "repo_unverified", "repo_not_selected", "repo_not_available",
                     "repo_control_not_found", "script_outdated", "invalid_repo"}


class ArenaAgentRunner:
    def __init__(self, adapter: ArenaAgentAdapter | None = None) -> None:
        self.adapter = adapter or ArenaAgentAdapter()
        self.github_poll_seconds = 30.0
        self.stall_minutes = float(STALL_MINUTES)
        self._github_checked: dict[str, float] = {}
        self._quiet_misses: dict[str, int] = {}
        # R4 live code: per session, the last published head, publish time, file patch hashes and summary.
        self.live_code_seconds = LIVE_CODE_MIN_SECONDS
        self._live_head: dict[str, str] = {}
        self._live_at: dict[str, float] = {}
        self._live_files: dict[str, dict[str, int]] = {}
        self._live_summary: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _project(db: AsyncSession, task: Task) -> Project | None:
        return await db.get(Project, task.project_id)

    @staticmethod
    def _audit(db: AsyncSession, actor: str, action: str, task: Task | None, details: dict[str, Any]) -> None:
        db.add(AuditLog(actor=actor, action=action, resource_type="task", resource_id=task.id if task else None, details=details))

    async def _notify(self, db: AsyncSession, title: str, message: str, *, severity: str = "warning", link: str = "/browsers") -> None:
        db.add(Notification(category="arena", severity=severity, title=title[:200], message=message, link=link))

    async def _block_needs_human(self, db: AsyncSession, session: ArenaSession, task: Task, reason: str) -> None:
        """Stop this session and hand it to a human (resolved in the browser, then Resume)."""
        agent = await db.get(Agent, session.agent_id)
        reason = reason[:200]
        session.phase = "needs_human"
        session.needs_human_reason = reason
        append_event(session, "needs_human", reason)
        if task.status not in {"blocked", "cancelled", "done", "merged"}:
            task.status = "blocked"
            task.lock_owner = None
            task.locked_at = None
            task.checkpoints = list(task.checkpoints or []) + [
                {"ts": _now().isoformat(), "event": "needs_human", "browser_profile_id": session.profile_id, "reason": reason,
                 "arena_session_id": session.id}
            ]
        if agent is not None:
            if agent.current_task_id == task.id:
                agent.current_task_id = None
            agent.status = "needs_human"
        profile = await db.get(BrowserProfile, session.profile_id)
        if profile is not None:
            profile.connector_status = "needs_human"
            profile.needs_human_reason = reason
        await self._notify(db, f"{agent.name if agent else session.agent_id}: Arena session needs you",
                           f"Profile {session.profile_id} stopped ({reason}). Resolve it in that browser window, then press Resume.",
                           severity="critical")
        db.add(AuditLog(actor=session.agent_id, action="arena.needs_human", resource_type="browser_profile",
                        resource_id=session.profile_id, details={"task_id": task.id, "reason": reason, "session_id": session.id},
                        severity="warning"))
        await db.commit()
        await event_bus.emit("arena.needs_human", {"agent_id": session.agent_id, "profile_id": session.profile_id,
                                                   "task_id": task.id, "reason": reason})

    async def _escalate(self, db: AsyncSession, session: ArenaSession, task: Task, reason: str) -> None:
        session.phase = "escalated"
        session.error_message = reason
        append_event(session, "escalated", reason)
        task.status = "blocked"
        task.error_message = reason
        task.lock_owner = None
        task.locked_at = None
        task.checkpoints = list(task.checkpoints or []) + [{"ts": _now().isoformat(), "event": "arena_escalated", "reason": reason}]
        agent = await db.get(Agent, session.agent_id)
        if agent is not None and agent.current_task_id == task.id:
            agent.current_task_id = None
            agent.status = "idle"
        await self._notify(db, f"Arena task escalated: {task.title}", reason, severity="critical", link="/tasks")
        self._audit(db, "arena-runner", "arena.task_escalated", task, {"reason": reason, "session_id": session.id})
        await db.commit()
        await event_bus.emit("arena.escalated", {"task_id": task.id, "session_id": session.id, "reason": reason})

    # ------------------------------------------------------------------ AgentLoop entry point
    async def run_message(self, db: AsyncSession, message: AgentMessage, task: Task, agent: Agent) -> None:
        """Start (or resume) the employee's Arena session for this task and submit the prompt."""
        from app.runtime.loop.common import NeedsHumanError

        if task.task_type == "follow_up":
            await self._start_correction(db, message, task, agent)
            return
        try:
            session, fresh = await claim_session(db, agent, task)
        except ArenaSessionConflict as exc:
            raise ArenaRunnerError(str(exc)) from exc
        session.active_task_id = task.id
        profile_id = session.profile_id
        try:
            if fresh:
                session.phase = "starting"
                append_event(session, "new_session")
                await db.commit()
                project = await self._project(db, task)
                repo = repo_slug(project)
                if not repo:
                    raise ArenaAgentError("repo_unknown", "The project has no GitHub repository URL, so Arena cannot be pointed at it.")
                await self.adapter.new_session(profile_id)
                await self.adapter.select_repo(profile_id, repo)
                append_event(session, "repo_selected", repo)
                prompt = build_task_prompt(task, project, agent)
                session.phase = "submitting"
                await db.commit()
                await self.adapter.submit(profile_id, prompt, repo=repo)
                session.submitted_at = _now()
                session.last_state_change_at = session.submitted_at
                session.phase = "working"
                append_event(session, "submitted", f"{len(prompt)} chars", title_prefix=pr_title_prefix(task.id))
                self._audit(db, agent.id, "arena.task_submitted", task, {"session_id": session.id, "prompt_chars": len(prompt)})
            else:
                session.phase = "working" if not session.pr_number else "reporting"
                session.needs_human_reason = None
                session.last_state_change_at = _now()  # the stall timer restarts after a human resolved the page
                append_event(session, "monitoring_resumed")
        except ArenaAgentError as exc:
            session.error_message = f"{exc.code}: {exc.message}"[:500]
            append_event(session, "submit_failed", session.error_message)
            if exc.code == "needs_human" or exc.code in REPO_SAFETY_CODES:
                session.phase = "needs_human"
                session.needs_human_reason = exc.message[:240]
                await db.commit()
                raise NeedsHumanError(profile_id, exc.message) from exc
            session.recovery_count += 1
            if session.recovery_count >= SUBMIT_FAILURES_BEFORE_HUMAN:
                session.phase = "needs_human"
                await db.commit()
                raise NeedsHumanError(profile_id, f"Arena submission failed {session.recovery_count} times ({exc.code}).") from exc
            session.phase = "new"
            await db.commit()
            raise ArenaRunnerError(f"Arena Agent {exc.code}: {exc.message}") from exc
        message.execution_status = "completed"
        message.response_payload = {**(message.response_payload or {}), "arena_session_id": session.id, "runtime": "arena_agent"}
        task.checkpoints = list(task.checkpoints or []) + [
            {"ts": _now().isoformat(), "event": "arena_submitted" if fresh else "arena_resumed", "arena_session_id": session.id}
        ]
        await db.commit()
        await event_bus.emit("arena.session", {"session_id": session.id, "agent_id": agent.id, "task_id": task.id, "phase": session.phase})

    async def _start_correction(self, db: AsyncSession, message: AgentMessage, follow_up: Task, agent: Agent) -> None:
        from app.runtime.loop.common import NeedsHumanError

        parent = await db.get(Task, follow_up.parent_task_id) if follow_up.parent_task_id else None
        session = await session_for_task(db, parent.id) if parent else None
        if parent is None or session is None or not session.is_open or session.agent_id != agent.id:
            raise ArenaRunnerError("No open Arena session for this pull request; the correction cannot reach the agent.")
        if session.active_task_id == follow_up.id:
            # Already submitted (resume after a human check or a restart): keep monitoring.
            session.phase = "correcting"
            session.needs_human_reason = None
            session.last_state_change_at = _now()
            append_event(session, "monitoring_resumed")
            message.execution_status = "completed"
            await db.commit()
            return
        project = await self._project(db, parent)
        changes = await self._compare(project, session)
        discrepancies = compare_report_to_changes(session.report or "", [f["filename"] for f in changes.get("files", [])],
                                                  commits=changes.get("ahead_by") if changes.get("ok") else None)
        prompt = build_correction_prompt(parent, review_comments=follow_up.description or "", discrepancies=discrepancies,
                                         round_number=parent.review_round, pr_url=session.pr_url)
        try:
            await self.adapter.submit(session.profile_id, prompt, repo=repo_slug(project))
        except ArenaAgentError as exc:
            append_event(session, "correction_failed", f"{exc.code}: {exc.message}")
            await db.commit()
            if exc.code == "needs_human" or exc.code in REPO_SAFETY_CODES:
                raise NeedsHumanError(session.profile_id, exc.message) from exc
            raise ArenaRunnerError(f"Arena Agent {exc.code}: {exc.message}") from exc
        session.correction_rounds += 1
        session.follow_up_count += 1
        session.phase = "correcting"
        session.active_task_id = follow_up.id
        session.last_state_change_at = _now()
        append_event(session, "correction_submitted", f"round {parent.review_round}", head_sha=session.head_sha,
                     discrepancies=len(discrepancies))
        self._quiet_misses.pop(session.id, None)
        message.execution_status = "completed"
        message.response_payload = {**(message.response_payload or {}), "arena_session_id": session.id, "discrepancies": discrepancies}
        self._audit(db, agent.id, "arena.correction_submitted", parent, {"session_id": session.id, "round": parent.review_round,
                                                                         "discrepancies": len(discrepancies)})
        await db.commit()

    # ------------------------------------------------------------------ GitHub
    async def _compare(self, project: Project | None, session: ArenaSession) -> dict[str, Any]:
        slug = repo_slug(project)
        if not slug or not session.branch_name:
            return {"ok": False, "files": []}
        base = project_base_branch(project)
        return await git_workflow_manager.compare_branch(repo_slug=slug, base=base, head=session.branch_name,
                                                         cwd=project.repo_path if project else ".")

    def _github_due(self, session: ArenaSession, *, force: bool) -> bool:
        last = self._github_checked.get(session.id, 0.0)
        if force or time.monotonic() - last >= self.github_poll_seconds:
            self._github_checked[session.id] = time.monotonic()
            return True
        return False

    async def _poll_github(self, db: AsyncSession, session: ArenaSession, parent: Task, project: Project | None) -> dict[str, Any]:
        """Detect the task's PR and track new commits on its branch (GitHub API only)."""
        slug = repo_slug(project)
        if not slug:
            return {"ok": False, "reason": "project has no GitHub repository"}
        cwd = project.repo_path if project else "."
        found = await git_workflow_manager.find_task_pull_request(repo_slug=slug, task_id=parent.id, cwd=cwd)
        pr = found.get("pull_request") if found.get("ok") else None
        if pr is None:
            # No PR yet: watch arena/* branches so pushes count as activity (stall detection).
            branches = await git_workflow_manager.list_arena_branches(repo_slug=slug, cwd=cwd)
            taken = set((await db.execute(
                select(ArenaSession.branch_name).where(ArenaSession.branch_name.is_not(None), ArenaSession.id != session.id)
            )).scalars())
            seen = {e.get("branch"): e.get("sha") for e in (session.events or []) if e.get("kind") == "arena_branch_push"}
            for b in branches.get("branches", []):
                if b["name"] in taken or not b.get("sha") or seen.get(b["name"]) == b["sha"]:
                    continue
                append_event(session, "arena_branch_push", b["name"], branch=b["name"], sha=b["sha"])
                if b["name"] in seen:  # a branch that moved since the last check = new commits
                    session.last_state_change_at = _now()
            return {"ok": found.get("ok", False), "pr": None}
        if pr.get("state") == "MERGED" and parent.status not in {"approved", "merged", "done"}:
            await self._merged_without_approval(db, session, parent, pr)
            return {"ok": True, "pr": pr, "merged_outside": True}
        expected_base = configured_base_branch(project)
        if expected_base and pr.get("baseRefName") != expected_base:
            await self._wrong_base_pull_request(db, session, parent, pr, expected_base)
            return {"ok": True, "pr": pr, "wrong_base": True}
        if session.pr_number != pr.get("number"):
            await self._associate_pr(db, session, parent, project, pr)
        head = pr.get("headRefOid")
        if head and head != session.head_sha:
            previous = session.head_sha
            session.head_sha = head
            session.last_state_change_at = _now()
            append_event(session, "new_commits", head[:12], previous=previous[:12] if previous else None)
            await event_bus.emit("arena.commits", {"session_id": session.id, "task_id": parent.id, "agent_id": session.agent_id,
                                                   "branch": session.branch_name, "head_sha": head})
        if head and self._live_head.get(session.id) != head:
            await self.publish_live_code(session, parent, project)
        return {"ok": True, "pr": pr}

    # ------------------------------------------------------------------ R4 live code
    async def publish_live_code(self, session: ArenaSession, parent: Task, project: Project | None, *, force: bool = False) -> dict[str, Any]:
        """Stream the branch's changes against the base as redacted ``agent.code`` frames (throttled)."""
        from app.runtime.code_stream import build_github_frame, code_stream

        now = time.monotonic()
        if not force and now - self._live_at.get(session.id, -1e9) < self.live_code_seconds:
            return {"throttled": True}
        changes = await self._compare(project, session)
        if not changes.get("ok"):
            return {"ok": False}
        self._live_at[session.id] = now
        self._live_head[session.id] = changes.get("head_sha") or session.head_sha or ""
        seen = self._live_files.setdefault(session.id, {})
        published = 0
        for f in changes.get("files", []):
            digest = hash((f["status"], f.get("patch", ""), f["additions"], f["deletions"]))
            if seen.get(f["filename"]) == digest or published >= LIVE_CODE_MAX_FILES:
                continue  # unchanged since the last publish: no repeated update
            seen[f["filename"]] = digest
            frame = build_github_frame(
                agent_id=session.agent_id, task_id=parent.id, branch=session.branch_name or "", filename=f["filename"],
                patch=f.get("patch", ""), file_status=f["status"], additions=f["additions"], deletions=f["deletions"],
                head_sha=session.head_sha, compare_url=changes.get("html_url"),
            )
            if frame is not None:
                await code_stream.publish(frame)
                published += 1
        summary = live_diff_summary(session, parent, changes)
        self._live_summary[session.id] = summary
        await event_bus.emit("arena.live_diff", {k: v for k, v in summary.items() if k != "files"} | {"files": summary["files"][:50]})
        return {"ok": True, "published": published, "files": len(changes.get("files", []))}

    async def live_diff(self, db: AsyncSession, session: ArenaSession, *, refresh: bool = False) -> dict[str, Any]:
        """Latest live-diff summary for the dashboard (refreshed from GitHub on request)."""
        parent = await db.get(Task, session.task_id) if session.task_id else None
        if parent is None:
            return {"ok": False, "error": "Task not found."}
        if refresh or session.id not in self._live_summary:
            if not session.branch_name:
                return {"ok": False, "error": "No GitHub branch is associated with this session yet.", "session_id": session.id}
            changes = await self._compare(await self._project(db, parent), session)
            if not changes.get("ok"):
                return {"ok": False, "error": "GitHub compare failed.", "session_id": session.id}
            self._live_summary[session.id] = live_diff_summary(session, parent, changes)
        return {"ok": True, **self._live_summary[session.id]}

    async def _associate_pr(self, db: AsyncSession, session: ArenaSession, parent: Task, project: Project | None, pr: dict[str, Any]) -> None:
        session.pr_number = int(pr.get("number") or 0) or None
        session.pr_url = str(pr.get("url") or "")[:500] or None
        session.branch_name = str(pr.get("headRefName") or "")[:160] or None
        append_event(session, "pr_detected", session.pr_url, branch=session.branch_name)
        parent.pr_number = session.pr_number
        parent.pr_url = session.pr_url
        parent.branch_name = session.branch_name
        parent.pr_status = "draft" if pr.get("isDraft") else "open"
        wf = (await db.execute(select(GitWorkflowRecord).where(GitWorkflowRecord.task_id == parent.id))).scalar_one_or_none()
        if wf is None:
            wf = GitWorkflowRecord(project_id=parent.project_id, task_id=parent.id, branch_name=session.branch_name or "",
                                   owner_agent_id=session.agent_id,
                                   base_branch=project_base_branch(project))
            db.add(wf)
        wf.branch_name = session.branch_name or wf.branch_name
        wf.owner_agent_id = session.agent_id
        wf.pr_number = session.pr_number
        wf.pr_url = session.pr_url
        wf.pr_status = parent.pr_status
        wf.last_commit_sha = pr.get("headRefOid")
        self._audit(db, "arena-runner", "arena.pr_detected", parent, {"pr_number": session.pr_number, "branch": session.branch_name,
                                                                       "agent_id": session.agent_id})
        await event_bus.emit("arena.pr_detected", {"task_id": parent.id, "agent_id": session.agent_id, "pr_url": session.pr_url,
                                                   "branch": session.branch_name})

    async def _merged_without_approval(self, db: AsyncSession, session: ArenaSession, parent: Task, pr: dict[str, Any]) -> None:
        reason = f"PR {pr.get('url')} was merged without the owner's approval in AATM."
        if any(e.get("kind") == "merged_outside_approval" for e in session.events or []):
            return
        append_event(session, "merged_outside_approval", reason)
        session.phase = "escalated"
        self._audit(db, "arena-runner", "arena.pr_merged_without_approval", parent, {"pr": pr.get("url")})
        await self._notify(db, "Pull request merged without approval", reason, severity="critical", link="/approvals")
        await db.commit()

    async def _wrong_base_pull_request(self, db: AsyncSession, session: ArenaSession, parent: Task, pr: dict[str, Any],
                                       expected_base: str) -> None:
        if any(e.get("kind") == "pr_wrong_base" for e in session.events or []):
            return
        reason = f"PR {pr.get('url')} targets {pr.get('baseRefName')}, but this project only accepts {expected_base}."
        append_event(session, "pr_wrong_base", reason)
        session.phase = "escalated"
        self._audit(db, "arena-runner", "arena.pr_wrong_base", parent, {"pr": pr.get("url"), "base": pr.get("baseRefName")})
        await self._notify(db, "Pull request targets the wrong branch", reason, severity="critical", link="/approvals")
        await db.commit()

    # ------------------------------------------------------------------ monitoring
    async def tick(self, db: AsyncSession) -> dict[str, Any]:
        """Advance every open Arena session one step (called each orchestration cycle)."""
        session_ids = list((await db.execute(
            select(ArenaSession.id).where(ArenaSession.is_open.is_(True), ArenaSession.phase.in_(MONITORED_PHASES))
        )).scalars())
        results: dict[str, Any] = {}
        for session_id in session_ids:
            try:
                session = await db.get(ArenaSession, session_id)  # re-loaded: a rollback expires earlier rows
                if session is not None:
                    results[session_id] = await self._tick_session(db, session)
            except Exception as exc:  # noqa: BLE001 - one broken session must not stop the others
                logger.warning("Arena session %s tick failed: %s", session_id, type(exc).__name__, exc_info=True)
                await db.rollback()
                results[session_id] = {"error": type(exc).__name__}
        return results

    async def _tick_session(self, db: AsyncSession, session: ArenaSession) -> dict[str, Any]:
        parent = await db.get(Task, session.task_id) if session.task_id else None
        active = await db.get(Task, session.active_task_id) if session.active_task_id else parent
        if parent is None or active is None:
            await close_session(db, session, phase="closed", reason="task removed")
            await db.commit()
            return {"closed": True}
        if active.status in {"blocked", "cancelled"}:
            return {"skipped": active.status}
        project = await self._project(db, parent)
        previous_state = session.page_state
        state = await self.adapter.state(session.profile_id)
        changed = apply_bridge_state(session, state)
        if changed and previous_state == "offline" and session.page_state not in {"offline", "needs_human"}:
            session.recovery_count += 1
            append_event(session, "recovered", f"monitoring restored ({session.page_state})")
        active.updated_at = _now()  # heartbeat: the task is being monitored, not stalled
        page = session.page_state

        if page == "needs_human":
            await self._block_needs_human(db, session, active, session.needs_human_reason or "human check on the Arena page")
            return {"phase": session.phase}

        # The agent says it cannot reach GitHub: Keep working or nudges cannot help, a human must connect it.
        if not session.pr_number and session.submitted_at is not None and (
            page == "keep_working_visible" or (changed and page in {"idle", "ready"})
        ) and await self._reports_github_blocked(session):
            slug = repo_slug(project) or "the project repository"
            await self._block_needs_human(db, session, active, f"Arena agent has no GitHub access to {slug}: connect GitHub for this account")
            return {"phase": session.phase, "github_blocked": True}

        reference = max(filter(None, [_naive(session.last_state_change_at), _naive(session.submitted_at)]), default=None)
        if reference and _naive(_now()) - reference > timedelta(minutes=self.stall_minutes) and not (page in {"idle", "ready"} and session.pr_number):
            await self._block_needs_human(db, session, active, f"stalled: no Arena activity or new commits for {int(self.stall_minutes)} minutes")
            return {"phase": session.phase, "stalled": True}

        github: dict[str, Any] = {}
        if page == "keep_working_visible":
            # Arena shows this option after every reply, so its presence does not mean the agent stopped early.
            decision, github = await self._keep_working_decision(db, session, parent, project)
            if decision == "merged_outside":
                return {"phase": session.phase}
            if decision == "press":
                try:
                    clicked = await self.adapter.keep_working(session.profile_id)
                except ArenaAgentError as exc:
                    append_event(session, "keep_working_failed", f"{exc.code}: {exc.message}")
                    await db.commit()
                    return {"phase": session.phase, "error": exc.code}
                if clicked.get("clicked"):
                    session.keep_working_count += 1
                    append_event(session, "keep_working", f"#{session.keep_working_count}")
                await db.commit()
                return {"phase": session.phase, "keep_working": session.keep_working_count}
            if decision == "limit":
                if not any(e.get("kind") == "keep_working_limit" for e in session.events or []):
                    append_event(session, "keep_working_limit", f"limit {session.keep_working_count} reached")
                    await self._notify(db, f"Keep working limit reached: {parent.title}",
                                       "ARENA_AGENT_MAX_KEEP_WORKING was reached; AATM stopped continuing this session.")
            else:
                skipped = [e for e in session.events or [] if e.get("kind") == "keep_working_skipped"]
                if not skipped or skipped[-1].get("detail") != decision:
                    append_event(session, "keep_working_skipped", decision)

        # Quiet = the agent stopped: idle, or Keep working shown when it must not be pressed.
        quiet = page in {"idle", "ready", "keep_working_visible"}
        if not github and session.submitted_at is not None and self._github_due(session, force=quiet and changed):
            github = await self._poll_github(db, session, parent, project)
            if github.get("merged_outside"):
                return {"phase": session.phase}
        if not quiet or page == "offline":
            await db.commit()
            return {"phase": session.phase, "page": page}

        if not session.pr_number:
            if not github:
                await db.commit()
                return {"phase": session.phase, "waiting": "github"}
            misses = self._quiet_misses.get(session.id, 0) + 1
            self._quiet_misses[session.id] = misses
            if misses < NO_PR_QUIET_CHECKS:
                await db.commit()
                return {"phase": session.phase, "waiting": "pr"}
            nudges = sum(1 for e in session.events or [] if e.get("kind") == "pr_nudge")
            if nudges >= NO_PR_NUDGES:
                await self._escalate(db, session, active, f"Arena Agent stopped without a pull request titled {pr_title_prefix(parent.id)} "
                                                          f"after {nudges} follow-ups.")
                return {"phase": session.phase}
            await self._follow_up(db, session, "pr_nudge", "\n".join([
                f"You have not opened the pull request for {pr_title_prefix(parent.id)} yet.",
                f"Push your work and open ONE pull request whose title starts with {pr_title_prefix(parent.id)}. Do NOT merge it.",
                *base_branch_publish_steps(project),
            ]))
            self._quiet_misses[session.id] = 0
            return {"phase": session.phase, "nudged": nudges + 1}

        self._quiet_misses.pop(session.id, None)
        return await self._collect_report(db, session, parent, active, project)

    async def _keep_working_decision(self, db: AsyncSession, session: ArenaSession, parent: Task,
                                     project: Project | None) -> tuple[str, dict[str, Any]]:
        """Return ``press`` only for an agent that stopped mid-task, otherwise the reason not to press."""
        if session.pr_number:
            return "pr_exists", {}
        try:
            reply = await self.adapter.read_last_reply(session.profile_id)
        except ArenaAgentError as exc:
            append_event(session, "read_reply_failed", f"{exc.code}: {exc.message}")
            return "reply_unread", {}
        text = str(reply.get("text") or "")
        if not text.strip():
            return "reply_unread", {}
        if reply_has_final_report(text):
            return "final_report", {}
        if not keep_working_allowed(session.keep_working_count):
            return "limit", {}
        github = await self._poll_github(db, session, parent, project)
        if github.get("merged_outside"):
            return "merged_outside", github
        if session.pr_number:
            return "pr_exists", github
        if not github.get("ok") and repo_slug(project):
            return "github_unknown", github
        return "press", github

    async def _reports_github_blocked(self, session: ArenaSession) -> bool:
        try:
            reply = await self.adapter.read_last_reply(session.profile_id)
        except ArenaAgentError as exc:
            append_event(session, "read_reply_failed", f"{exc.code}: {exc.message}")
            return False
        return reports_github_blocked(str(reply.get("text") or ""))

    async def _follow_up(self, db: AsyncSession, session: ArenaSession, kind: str, text: str) -> bool:
        parent = await db.get(Task, session.task_id) if session.task_id else None
        repo = repo_slug(await self._project(db, parent)) if parent else None
        try:
            await self.adapter.submit(session.profile_id, text, repo=repo)
        except ArenaAgentError as exc:
            append_event(session, f"{kind}_failed", f"{exc.code}: {exc.message}")
            await db.commit()
            return False
        session.follow_up_count += 1
        session.last_state_change_at = _now()
        append_event(session, kind)
        await db.commit()
        return True

    async def _collect_report(self, db: AsyncSession, session: ArenaSession, parent: Task, active: Task, project: Project | None) -> dict[str, Any]:
        """Quiet session with a PR: read the agent's report, then hand the PR to the reviewer."""
        is_correction = active.task_type == "follow_up"
        if is_correction:
            submitted = [e for e in session.events or [] if e.get("kind") == "correction_submitted"]
            head_before = submitted[-1].get("head_sha") if submitted else None
            if head_before and head_before == session.head_sha:
                nudged = [e for e in session.events or [] if e.get("kind") == "no_new_commits_nudge" and e["ts"] >= submitted[-1]["ts"]]
                if not nudged:
                    await self._follow_up(db, session, "no_new_commits_nudge",
                                          f"No new commits reached the pull request for {pr_title_prefix(parent.id)}. "
                                          "Push the corrections to the same branch, then send the updated report.")
                    return {"phase": session.phase, "nudged": "commits"}
        try:
            reply = await self.adapter.read_last_reply(session.profile_id)
        except ArenaAgentError as exc:
            append_event(session, "read_reply_failed", f"{exc.code}: {exc.message}")
            await db.commit()
            return {"phase": session.phase, "error": exc.code}
        text = redact_secrets(reply.get("text") or "")[:MAX_REPORT_CHARS]
        requested = [e for e in session.events or [] if e.get("kind") == "report_requested"]
        last_submit = max((e["ts"] for e in session.events or [] if e.get("kind") in {"submitted", "correction_submitted"}), default="")
        if not report_is_complete(text) and not any(e["ts"] >= last_submit for e in requested):
            session.phase = "reporting"
            await self._follow_up(db, session, "report_requested", build_report_request(parent))
            return {"phase": session.phase, "report": "requested"}
        session.report = text or "(the agent sent no report)"
        append_event(session, "report_received", f"{len(text)} chars", complete=report_is_complete(text))
        changes = await self._compare(project, session)
        files = [f["filename"] for f in changes.get("files", [])] if changes.get("ok") else []
        if project and session.branch_name:
            checkout = await git_workflow_manager.checkout_review_worktree(repo_path=project.repo_path, branch_name=session.branch_name,
                                                                            task_id=parent.id)
            if checkout.get("ok"):
                parent.worktree_path = checkout["worktree_path"]
            else:
                append_event(session, "review_checkout_failed", str(checkout.get("error") or "")[:200])
        parent.actual_deliverables = files or list(parent.actual_deliverables or [])
        parent.commit_sha = session.head_sha or parent.commit_sha
        session.phase = "reviewing"
        session.active_task_id = parent.id
        summary = f"Arena Agent report ({len(text)} chars) for PR {session.pr_url}:\n\n{text[:4000]}"
        from app.orchestration.review import review_service
        from app.orchestration.scheduler import task_scheduler

        if is_correction:
            active.checkpoints = list(active.checkpoints or []) + [{"ts": _now().isoformat(), "event": "arena_correction_done"}]
            await task_scheduler.complete_task(db, active, summary=summary, target_status="completed")
            await review_service.on_follow_up_completed(db, active)
        else:
            parent.checkpoints = list(parent.checkpoints or []) + [{"ts": _now().isoformat(), "event": "arena_report_received",
                                                                    "pr_url": session.pr_url}]
            await task_scheduler.complete_task(db, parent, summary=summary, target_status="pr_open")
        await event_bus.emit("arena.report", {"session_id": session.id, "task_id": parent.id, "agent_id": session.agent_id})
        return {"phase": session.phase, "report": "stored", "files": len(files)}

    # ------------------------------------------------------------------ review / merge hooks
    async def review_material(self, db: AsyncSession, parent: Task) -> dict[str, Any] | None:
        """What the reviewer must verify for an Arena-built PR: the report vs the real GitHub diff."""
        from app.core.security import sanitize_untrusted_content

        session = await session_for_task(db, parent.id)
        if session is None:
            return None
        project = await self._project(db, parent)
        changes = await self._compare(project, session)
        files = changes.get("files", []) if changes.get("ok") else []
        report = session.report or ""
        return {
            "agent_report": sanitize_untrusted_content(report, source=f"arena_report:{parent.id}")["quarantined_text"],
            "pull_request": session.pr_url,
            "branch": session.branch_name,
            "head_sha": session.head_sha,
            "compare_url": changes.get("html_url"),
            "commits": changes.get("commits", [])[-30:],
            "changed_files": [{k: f[k] for k in ("filename", "status", "additions", "deletions")} for f in files],
            "github_diff": sanitize_untrusted_content("\n".join(f"--- {f['filename']}\n{f.get('patch', '')}" for f in files)[:20_000],
                                                      source="pull_request:github_compare")["quarantined_text"],
            "mechanical_discrepancies": compare_report_to_changes(report, [f["filename"] for f in files],
                                                                  commits=changes.get("ahead_by") if changes.get("ok") else None),
            "verify": ("Do not trust the agent's report. Compare every claim (modified files, completed requirements, tests and "
                       "results) with the actual diff, commits and code; run the tests in the review worktree. "
                       "Return REQUEST_CHANGES listing every discrepancy, or COMMENT only when the report matches the code."),
        }

    async def on_task_merged(self, db: AsyncSession, task: Task) -> None:
        session = await session_for_task(db, task.id)
        if session is not None and session.is_open:
            await close_session(db, session, phase="merged", reason="PR merged after owner approval; next task starts a new session")
        for cache in (self._github_checked, self._live_head, self._live_at, self._live_files):
            cache.pop(session.id if session else "", None)


arena_runner = ArenaAgentRunner()
