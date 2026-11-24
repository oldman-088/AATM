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

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.git_workflows.manager import git_workflow_manager
from app.github_client.client import validate_branch_name
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
from app.providers.adapters.arena_agent import (
    TOOL_LIMIT_CONTINUE,
    ArenaAgentAdapter,
    ArenaAgentError,
    keep_working_allowed,
)
from app.runtime.agent_protocol import redact_secrets
from app.runtime.temporal import to_naive_utc

logger = logging.getLogger(__name__)

MONITORED_PHASES = {"working", "correcting", "reporting"}
RESERVING_PHASES = MONITORED_PHASES | {"reviewing", "awaiting_approval", "starting", "submitting", "needs_human"}
# Sessions that wait for a human decision: they are not monitored any more, but their pull request
# can still be merged or closed outside AATM, so its state is polled separately.
PR_STATE_PHASES = ("reviewing", "awaiting_approval")
PR_STATE_POLL_SECONDS = 300.0  # poll the PR state of those sessions every 5 minutes
STALL_MINUTES = 20
STALL_CONTINUES = 2       # E39: "continue" messages sent to a quiet, unfinished agent before it is handed to a human
STALL_CONTINUE_PAGES = {"idle", "ready", "keep_working_visible", "tool_call_limit"}
NO_PR_NUDGES = 2          # follow-ups asking for the PR before the task is escalated to the owner
AGENT_ACTIVE_STATES = {"generating", "keep_working_visible", "tool_call_limit", "pr_ready"}
CREATE_PR_WAIT_MINUTES = 5  # after pressing Arena's Create PR, before falling back to asking the agent
NO_PR_QUIET_CHECKS = 2    # quiet GitHub checks without a PR before a nudge
SUBMIT_FAILURES_BEFORE_HUMAN = 3
MAX_REPORT_CHARS = 60_000
SENDABLE_TASK_STATUSES = {"queued", "assigned", "in_progress", "pr_open", "in_review", "changes_requested", "approved", "merged"}
BASE_HEAD_RETRY_DELAYS = (2.0, 6.0)  # E47: waits before the 2nd and 3rd base-head lookups
LIVE_CODE_MIN_SECONDS = 10.0   # at most one GitHub live-diff publish per session per interval
LIVE_CODE_MAX_FILES = 20       # files streamed per new head (the summary lists all of them)
FOLLOW_UP_FAILURES_BEFORE_HUMAN = 3  # failed follow-up sends in a row before the owner is asked to look
CHAT_CHECK_SECONDS = 300.0     # how often a quiet window is checked to still show the task's chat
CHAT_REOPEN_ATTEMPTS = 3       # navigations back to the task's chat before the owner is asked
# A chat URL has an id segment (e.g. /agent/01a1ef92-...); the empty new-session page does not.
CHAT_PATH_RE = re.compile(r"^(/[A-Za-z0-9_.-]+)+/([A-Za-z0-9-]{8,})/?$")


def _now() -> datetime:
    return utcnow()


def _naive(dt: datetime | None) -> datetime | None:
    return to_naive_utc(dt) if dt is not None else None


class ArenaRunnerError(RuntimeError):
    pass


# How long a session may wait for a person before its worker is released (see expire_stale_human_sessions).
HUMAN_WAIT_LIMIT = timedelta(minutes=30)

# A session whose task is in one of these states no longer holds its employee: the work is over
# or stopped, so the session must not keep the worker from taking new queued tasks.
RELEASING_TASK_STATES = {"done", "completed", "cancelled", "merged", "failed"}


async def arena_reservations(db: AsyncSession) -> dict[str, str]:
    """agent_id -> implementation task id for employees whose Arena session is still in use.

    The employee stays bound to that session (so corrections reach the same Arena session) until
    the PR is merged or the task is closed; only that task's follow-ups are dispatched to them.
    Sessions whose task is finished or blocked are ignored, so they cannot hold the worker forever.
    """
    rows = (await db.execute(
        select(ArenaSession, Task.status)
        .outerjoin(Task, Task.id == ArenaSession.task_id)
        .where(ArenaSession.is_open.is_(True), ArenaSession.phase.in_(RESERVING_PHASES))
    )).all()
    return {
        s.agent_id: s.task_id
        for s, task_status in rows
        if s.task_id and task_status not in RELEASING_TASK_STATES
    }


def task_chat_path(session: ArenaSession) -> str | None:
    """The Arena chat this session's work lives in, recorded after AATM sent the task prompt."""
    for event in reversed(session.events or []):
        if event.get("kind") in {"chat_path", "chat_url"}:
            path = str(event.get("detail") or "")
            return path if CHAT_PATH_RE.match(path) else None
    return None


def chat_path_matches_branch(path: str, branch: str | None) -> bool:
    """Arena names its branch arena/<last 8 characters of the chat id>-<repo>."""
    match = re.match(r"^arena/([A-Za-z0-9]{8})-", branch or "")
    return match is None or path.rstrip("/")[-8:] == match.group(1)


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
        try:
            self.pr_state_poll_seconds = float(get_settings().ARENA_PR_STATE_POLL_SECONDS)
        except Exception:  # noqa: BLE001 - the default interval is used when settings are unavailable
            self.pr_state_poll_seconds = float(PR_STATE_POLL_SECONDS)
        self._github_checked: dict[str, float] = {}
        self._pr_state_checked: dict[str, float] = {}
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
    async def _expected_base_head(project: Project | None) -> str:
        """Resolve the remote base SHA AATM embeds in each worker's mandatory preflight."""
        base = project_base_branch(project)
        valid, _ = validate_branch_name(base)
        if not valid:
            raise ArenaAgentError("invalid_base_branch", "The project's base branch name is invalid.")
        cwd = Path(project.repo_path if project and project.repo_path else ".")
        delays = (0.0, *BASE_HEAD_RETRY_DELAYS)
        err = ""
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            code, out, err = await git_workflow_manager._exec(
                ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{base}"], cwd,
            )
            match = re.match(r"^([0-9a-fA-F]{40,64})\s+refs/heads/", out)
            if code == 0 and match is not None:
                return match.group(1).lower()
            if code == 2:  # --exit-code: the branch does not exist on origin; retrying cannot help
                break
            logger.warning("base head lookup for origin/%s failed (attempt %d/%d): %s", base, attempt, len(delays), err[:200])
        raise ArenaAgentError("base_head_unavailable", err or f"Could not resolve origin/{base}.")

    async def _refresh_send_target(self, db: AsyncSession, session: ArenaSession, *, task_id: str | None = None,
                                   kind: str = "follow_up") -> tuple[ArenaSession | None, Task | None]:
        """Reload session/task state immediately before any page-mutating send or continue action."""
        current = await db.get(ArenaSession, session.id, populate_existing=True)
        if current is None:
            return None, None
        active_id = task_id or current.active_task_id or current.task_id
        active = await db.get(Task, active_id, populate_existing=True) if active_id else None
        parent = await db.get(Task, current.task_id, populate_existing=True) if current.task_id else active
        inactive = next((task for task in (active, parent) if task is not None and task.status not in SENDABLE_TASK_STATUSES), None)
        if active is None or parent is None or inactive is not None:
            status = inactive.status if inactive is not None else "missing"
            logger.info("dropped: task %s", status)
            append_event(current, "message_dropped", f"dropped: task {status}", message_kind=kind)
            await db.commit()
            return current, None
        return current, active

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
        if fresh and session.phase == "submitting" and await self._submit_landed(session):
            # Restarted right after sending: the page has the prompt, so it must not be sent again.
            session.submitted_at = _now()
            append_event(session, "submit_recovered")
            fresh = False
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
                expected_head = await self._expected_base_head(project)
                prompt = build_task_prompt(task, project, agent, expected_head_sha=expected_head)
                session.phase = "submitting"
                append_event(session, "submit_started", (await self.adapter.transcript_mark(profile_id)).get("mark"))
                await db.commit()
                session, send_task = await self._refresh_send_target(db, session, task_id=task.id, kind="initial_submit")
                if session is None or send_task is None:
                    message.execution_status = "cancelled"
                    await db.commit()
                    return
                await self.adapter.submit(profile_id, prompt, repo=repo)
                session.submitted_at = _now()
                session.last_state_change_at = session.submitted_at
                session.phase = "working"
                append_event(session, "submitted", f"{len(prompt)} chars", title_prefix=pr_title_prefix(task.id))
                self._chat_state()["unrecorded"].add(session.id)  # remember this chat once its URL is known
                self._audit(db, agent.id, "arena.task_submitted", task, {"session_id": session.id, "prompt_chars": len(prompt)})
            else:
                session.phase = "working" if not session.pr_number else "reporting"
                session.needs_human_reason = None
                session.last_state_change_at = _now()  # the stall timer restarts after a human resolved the page
                append_event(session, "monitoring_resumed")
        except ArenaAgentError as exc:
            session.error_message = f"{exc.code}: {exc.message}"[:500]
            append_event(session, "submit_failed", session.error_message)
            if exc.code == "repo_not_available" and fresh:
                # E43: this account cannot see the repository (nothing was sent). Close the session and let
                # another worker take the task, instead of stopping a healthy window for a human.
                from app.runtime.loop.common import RepoAccessError

                await close_session(db, session, phase="closed", reason=f"repo not available: {exc.message}"[:240])
                await db.commit()
                raise RepoAccessError(profile_id, repo_slug(await self._project(db, task)) or "", exc.message) from exc
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

    def _chat_state(self) -> dict[str, Any]:
        """In-memory chat bookkeeping (kept off ``__init__`` so test doubles built with ``__new__`` work)."""
        return self.__dict__.setdefault("_chat", {"unrecorded": set(), "checked": {}, "reopened": {}, "failures": {}})

    async def _record_chat_path(self, session: ArenaSession) -> None:
        """After AATM sent the prompt, store the chat's URL so the window can be brought back to it later."""
        pending = self._chat_state()["unrecorded"]
        if session.id not in pending:
            return
        if task_chat_path(session):
            pending.discard(session.id)
            return
        try:
            page = await self.adapter.selector_test(session.profile_id)
        except ArenaAgentError:
            return
        path = str((page.get("page") or {}).get("path") or "")
        if CHAT_PATH_RE.match(path) and chat_path_matches_branch(path, session.branch_name):
            append_event(session, "chat_path", path)
            pending.discard(session.id)

    async def _on_task_chat(self, db: AsyncSession, session: ArenaSession, active: Task, changed: bool) -> bool:
        """True when the window shows this task's chat (or that cannot be known).

        After a restart the window opens on Arena's start page, so replies and follow-ups would reach
        another chat. A quiet window is sent back to the recorded chat; a busy one is never touched."""
        path = task_chat_path(session)
        if not path:
            return True
        chat = self._chat_state()
        now = time.monotonic()
        if not changed and now - chat["checked"].get(session.id, float("-inf")) < CHAT_CHECK_SECONDS:
            return True
        try:
            page = await self.adapter.selector_test(session.profile_id)
        except ArenaAgentError as exc:
            append_event(session, "chat_check_failed", f"{exc.code}: {exc.message}")
            await db.commit()
            return False
        where = page.get("page") or {}
        current = str(where.get("path") or "")
        if current.rstrip("/") == path.rstrip("/"):
            chat["checked"][session.id] = now
            chat["reopened"].pop(session.id, None)
            return True
        chat["checked"].pop(session.id, None)
        if page.get("state") == "generating":  # never interrupt whatever runs there
            await self._block_needs_human(db, session, active, f"the window is busy in another chat ({current or 'unknown'}); "
                                                                 f"the task's chat is {path}")
            return False
        attempts = chat["reopened"].get(session.id, 0)
        if attempts >= CHAT_REOPEN_ATTEMPTS:
            await self._block_needs_human(db, session, active, f"the window stays on {current or 'another page'}; "
                                                                 f"open the task's chat {path} in it, then press Resume")
            return False
        origin = str(where.get("origin") or "")
        try:
            await self.adapter.open_chat(session.profile_id, f"{origin}{path}")
        except ArenaAgentError as exc:
            await self._block_needs_human(db, session, active, f"could not return the window to the task's chat {path} "
                                                                 f"({exc.code})")
            return False
        chat["reopened"][session.id] = attempts + 1
        append_event(session, "chat_reopened", f"{current or '?'} -> {path}")
        await db.commit()
        return False

    async def _submit_landed(self, session: ArenaSession) -> bool:
        started = [e for e in session.events or [] if e.get("kind") == "submit_started"]
        if not started:
            return False
        try:
            page = await self.adapter.transcript_mark(session.profile_id)
        except ArenaAgentError as exc:
            append_event(session, "submit_check_failed", exc.code)
            return False
        return page.get("state") in AGENT_ACTIVE_STATES or page.get("mark") != started[-1].get("detail")

    async def recover_after_restart(self, db: AsyncSession) -> dict[str, int]:
        """Called once at startup: downtime is not a stall, and unfinished sends are checked again."""
        sessions = list((await db.execute(select(ArenaSession).where(
            ArenaSession.is_open.is_(True), ArenaSession.kind != "manager",
            ArenaSession.phase.in_(MONITORED_PHASES | {"starting", "submitting"}),
        ))).scalars())
        monitored = requeued = 0
        for session in sessions:
            task = await db.get(Task, session.active_task_id or session.task_id) if session.task_id else None
            if task is None:
                continue
            if session.phase in MONITORED_PHASES:
                session.last_state_change_at = _now()
                task.updated_at = _now()  # self-healing must not treat the downtime as a dead task
                append_event(session, "monitoring_restored")
                monitored += 1
            elif task.status == "in_progress":
                task.status = "queued"
                task.lock_owner = None
                running = (await db.execute(select(AgentMessage).where(
                    AgentMessage.task_id == task.id, AgentMessage.execution_status == "running"))).scalars()
                for message in running:
                    message.execution_status = "cancelled"
                append_event(session, "requeued_after_restart")
                requeued += 1
        await db.commit()
        return {"monitored": monitored, "requeued": requeued}

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
        session, send_task = await self._refresh_send_target(db, session, task_id=follow_up.id, kind="correction_submit")
        if session is None or send_task is None:
            message.execution_status = "cancelled"
            await db.commit()
            return
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
            corrected = None
            if pr.get("state") == "OPEN":
                fixed = await git_workflow_manager.ensure_task_pull_request(
                    repo_slug=slug, task_id=parent.id, title=parent.title, branch=str(pr.get("headRefName") or ""),
                    base=expected_base, cwd=cwd,
                )
                if fixed.get("ok"):
                    append_event(session, "pr_base_corrected", ", ".join(fixed.get("actions") or []))
                    again = await git_workflow_manager.find_task_pull_request(repo_slug=slug, task_id=parent.id, cwd=cwd)
                    corrected = again.get("pull_request") if again.get("ok") else None
            if not corrected or corrected.get("baseRefName") != expected_base:
                await self._wrong_base_pull_request(db, session, parent, pr, expected_base)
                return {"ok": True, "pr": pr, "wrong_base": True}
            pr = corrected
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

    # ------------------------------------------------------------------ PR state outside AATM
    def _pr_state_due(self, session: ArenaSession, *, force: bool = False) -> bool:
        """The PR state of a waiting session is polled every `pr_state_poll_seconds` (5 minutes)."""
        # No entry yet means "never checked": a monotonic clock can be small on a freshly booted machine.
        last = self._pr_state_checked.get(session.id)
        if force or last is None or time.monotonic() - last >= self.pr_state_poll_seconds:
            self._pr_state_checked[session.id] = time.monotonic()
            return True
        return False

    async def poll_pr_states(self, db: AsyncSession, *, force: bool = False) -> dict[str, Any]:
        """Watch the pull request of the sessions that wait for a human decision.

        A PR merged outside AATM must still finish its task (merged, session closed, queued
        correction rounds cancelled); a PR closed without merging blocks the task with a reason.
        """
        session_ids = list((await db.execute(
            select(ArenaSession.id).where(ArenaSession.is_open.is_(True), ArenaSession.phase.in_(PR_STATE_PHASES))
        )).scalars())
        results: dict[str, Any] = {}
        for session_id in session_ids:
            try:
                session = await db.get(ArenaSession, session_id)  # re-loaded: a rollback expires earlier rows
                if session is None or not session.is_open:
                    continue
                if not self._pr_state_due(session, force=force):
                    continue
                results[session_id] = await self._check_pr_state(db, session)
            except Exception as exc:  # noqa: BLE001 - one broken session must not stop the others
                logger.warning("PR state poll failed for session %s: %s", session_id, type(exc).__name__, exc_info=True)
                await db.rollback()
                results[session_id] = {"error": type(exc).__name__}
        return results

    async def _check_pr_state(self, db: AsyncSession, session: ArenaSession) -> dict[str, Any]:
        """Read the PR state on GitHub and apply it to the task (merged / closed without merge)."""
        parent = await db.get(Task, session.task_id) if session.task_id else None
        if parent is None:
            return {"skipped": "no task"}
        if parent.status in {"merged", "done", "completed", "cancelled", "failed"}:
            return {"skipped": "task already finished"}
        project = await self._project(db, parent)
        slug = repo_slug(project)
        if not slug:
            return {"ok": False, "reason": "project has no GitHub repository"}
        found = await git_workflow_manager.find_task_pull_request(
            repo_slug=slug, task_id=parent.id, cwd=project.repo_path if project else ".",
        )
        pr = found.get("pull_request") if found.get("ok") else None
        if pr is None:
            return {"ok": bool(found.get("ok")), "pr": None}
        state = str(pr.get("state") or "").upper()
        if state == "MERGED":
            await self._pr_merged_outside(db, session, parent, pr)
            return {"ok": True, "pr": pr, "merged": True}
        if state == "CLOSED":
            await self._pr_closed_without_merge(db, session, parent, pr)
            return {"ok": True, "pr": pr, "closed": True}
        return {"ok": True, "pr": pr, "state": state}

    async def _pr_merged_outside(self, db: AsyncSession, session: ArenaSession, parent: Task, pr: dict[str, Any]) -> None:
        """The PR was merged outside AATM: the task is merged, the session ends, corrections stop."""
        reason = f"PR {pr.get('url')} was merged outside AATM."
        append_event(session, "pr_merged_outside", reason)
        parent.status = "merged"
        parent.pr_status = "merged"
        parent.progress_percent = 100
        parent.lock_owner = None
        parent.locked_at = None
        parent.completed_at = parent.completed_at or _now()
        parent.checkpoints = list(parent.checkpoints or []) + [
            {"ts": _now().isoformat(), "event": "pr_merged_outside", "pr_url": pr.get("url"), "pr_number": pr.get("number")}
        ]
        cancelled = await self._cancel_queued_corrections(db, parent)
        await close_session(db, session, phase="merged", reason=reason)
        self._pr_state_checked.pop(session.id, None)
        self._audit(db, "arena-runner", "arena.pr_merged_outside", parent,
                    {"pr": pr.get("url"), "pr_number": pr.get("number"), "cancelled_corrections": cancelled})
        await self._notify(db, "Pull request merged outside AATM", reason, severity="warning", link="/tasks")
        await db.commit()
        await event_bus.emit("task.updated", {"task_id": parent.id, "status": parent.status})

    async def _cancel_queued_corrections(self, db: AsyncSession, parent: Task) -> list[str]:
        """Cancel the correction rounds that are still queued for a merged pull request."""
        res = await db.execute(
            select(Task).where(
                Task.parent_task_id == parent.id,
                Task.task_type == "follow_up",
                Task.status.in_(["queued", "assigned"]),
            )
        )
        cancelled: list[str] = []
        for follow_up in res.scalars().all():
            follow_up.status = "cancelled"
            follow_up.lock_owner = None
            follow_up.locked_at = None
            follow_up.checkpoints = list(follow_up.checkpoints or []) + [
                {"ts": _now().isoformat(), "event": "cancelled_pr_merged_outside"}
            ]
            cancelled.append(follow_up.id)
        return cancelled

    async def _pr_closed_without_merge(self, db: AsyncSession, session: ArenaSession, parent: Task, pr: dict[str, Any]) -> None:
        """The PR was closed without merging: the task is blocked and a human decides what happens next."""
        reason = f"PR {pr.get('url')} was closed without being merged."
        if any(e.get("kind") == "pr_closed_without_merge" for e in session.events or []):
            return
        append_event(session, "pr_closed_without_merge", reason)
        parent.status = "blocked"
        parent.error_message = reason
        parent.lock_owner = None
        parent.locked_at = None
        parent.checkpoints = list(parent.checkpoints or []) + [
            {"ts": _now().isoformat(), "event": "pr_closed_without_merge", "pr_url": pr.get("url"), "reason": reason}
        ]
        session.phase = "escalated"
        self._audit(db, "arena-runner", "arena.pr_closed_without_merge", parent, {"pr": pr.get("url")})
        await self._notify(db, "Pull request closed without merging", reason, severity="warning", link="/tasks")
        await db.commit()
        await event_bus.emit("task.updated", {"task_id": parent.id, "status": parent.status})

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
    async def expire_stale_human_sessions(self, db: AsyncSession, now: datetime | None = None) -> list[str]:
        """Release workers whose session has waited for a human longer than HUMAN_WAIT_LIMIT.

        The task stays blocked (a person still has to look at it); only the worker is freed, so a
        forgotten check can no longer stop the whole team. Each release is written to the audit log.
        """
        now = to_naive_utc(now or _now())
        rows = list((await db.execute(
            select(ArenaSession).where(ArenaSession.is_open.is_(True), ArenaSession.phase == "needs_human")
        )).scalars())
        released: list[str] = []
        for session in rows:
            since = session.last_state_change_at or session.submitted_at
            if since is None or now - to_naive_utc(since) < HUMAN_WAIT_LIMIT:
                continue
            minutes = int(HUMAN_WAIT_LIMIT.total_seconds() // 60)
            reason = f"needs_human waited over {minutes} min; worker released, task stays blocked"
            await close_session(db, session, phase="closed", reason=reason)
            agent = await db.get(Agent, session.agent_id)
            if agent is not None and agent.status == "needs_human":
                agent.status = "idle"
            db.add(AuditLog(actor="system:arena-runner", action="arena.needs_human_expired", resource_type="arena_session",
                            resource_id=session.id, details={"agent_id": session.agent_id, "task_id": session.task_id,
                                                             "limit_minutes": minutes},
                            severity="warning"))
            released.append(session.agent_id)
        if released:
            await db.commit()
        return released

    async def tick(self, db: AsyncSession) -> dict[str, Any]:
        """Advance every open Arena session one step (called each orchestration cycle)."""
        results: dict[str, Any] = {}
        results["expired_human_waits"] = await self.expire_stale_human_sessions(db)
        # Sessions that wait for a human decision are not monitored any more, but their pull request
        # can still be merged or closed outside AATM: poll its state (throttled to every 5 minutes).
        results["pr_states"] = await self.poll_pr_states(db)
        session_ids = list((await db.execute(
            select(ArenaSession.id).where(ArenaSession.is_open.is_(True), ArenaSession.phase.in_(MONITORED_PHASES))
        )).scalars())
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
        if active.status in {"blocked", "cancelled"} or parent.status in {"blocked", "cancelled"}:
            if active.status == "cancelled" or parent.status == "cancelled":
                try:
                    page_state = await self.adapter.state(session.profile_id)
                    if page_state.get("agent_state") == "generating":
                        await self.adapter.stop(session.profile_id)
                        append_event(session, "cancel_stop", "Arena generation stopped after task cancellation")
                        await db.commit()
                except ArenaAgentError as exc:
                    append_event(session, "cancel_stop_failed", f"{exc.code}: {exc.message}")
                    await db.commit()
            return {"skipped": active.status if active.status in {"blocked", "cancelled"} else parent.status}
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

        await self._record_chat_path(session)
        if page not in {"generating", "offline"} and not await self._on_task_chat(db, session, active, changed):
            return {"phase": session.phase, "chat": "returning"}

        # The agent says it cannot reach GitHub: Keep working or nudges cannot help, a human must connect it.
        if not session.pr_number and session.submitted_at is not None and (
            page in {"keep_working_visible", "tool_call_limit"} or (changed and page in {"idle", "ready"})
        ) and await self._reports_github_blocked(session):
            slug = repo_slug(project) or "the project repository"
            await self._block_needs_human(db, session, active, f"Arena agent has no GitHub access to {slug}: connect GitHub for this account")
            return {"phase": session.phase, "github_blocked": True}

        reference = max(filter(None, [_naive(session.last_state_change_at), _naive(session.submitted_at)]), default=None)
        if reference and page == "generating" and (
            timedelta(minutes=self.stall_minutes / 2) < _naive(_now()) - reference <= timedelta(minutes=self.stall_minutes)
        ):
            await self._note_page_mark(session, reference)
        if reference and _naive(_now()) - reference > timedelta(minutes=self.stall_minutes) and not (page in {"idle", "ready"} and session.pr_number):
            if page == "generating" and await self._page_progressed(session, reference):
                session.last_state_change_at = _now()  # a long turn keeps "generating" while it really works
                await db.commit()
                return {"phase": session.phase, "page": page}
            if page == "generating" and session.pr_number and await self._report_on_page(session):
                # The page still shows Stop, but the work is done: a PR exists and the report has not changed.
                append_event(session, "finished_while_generating")
                await self._close_feedback_once(session)
                return await self._collect_report(db, session, parent, active, project)
            if page in STALL_CONTINUE_PAGES and not session.pr_number:
                # E39: the agent stopped before finishing. Tell it to continue (as the old Auto Continue userscript
                # did) before asking a human; each send restarts the stall timer.
                sent = sum(1 for e in session.events or [] if e.get("kind") == "stall_continue")
                if sent < STALL_CONTINUES and await self._follow_up(db, session, "stall_continue", TOOL_LIMIT_CONTINUE):
                    return {"phase": session.phase, "stall_continue": sent + 1}
            await self._block_needs_human(db, session, active, f"stalled: no Arena activity or new commits for {int(self.stall_minutes)} minutes")
            return {"phase": session.phase, "stalled": True}

        github: dict[str, Any] = {}
        if page == "tool_call_limit":
            handled, github = await self._handle_tool_call_limit(db, session, parent, active, project)
            if handled is not None:
                return handled
        if page == "keep_working_visible":
            # Arena shows this option after every reply, so its presence does not mean the agent stopped early.
            decision, github = await self._keep_working_decision(db, session, parent, project)
            if decision == "merged_outside":
                return {"phase": session.phase}
            if decision == "press":
                session, send_task = await self._refresh_send_target(db, session, kind="keep_working")
                if session is None or send_task is None:
                    return {"phase": session.phase if session else "closed", "dropped": True}
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
        quiet = page in {"idle", "ready", "keep_working_visible", "tool_call_limit", "pr_ready"}
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
            secured = await self._secure_pull_request(db, session, parent, project, page)
            if secured is not None:
                await db.commit()
                return {"phase": session.phase, **secured}
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
        await self._close_feedback_once(session)
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

    async def _handle_tool_call_limit(self, db: AsyncSession, session: ArenaSession, parent: Task, active: Task,
                                      project: Project | None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Arena paused the agent mid-turn: continue it under the Keep working safeguards."""
        decision, github = await self._keep_working_decision(db, session, parent, project)
        if decision == "merged_outside":
            return {"phase": session.phase}, github
        if decision == "press":
            if await self._follow_up(db, session, "tool_limit_continue", TOOL_LIMIT_CONTINUE):
                session.keep_working_count += 1
                await db.commit()
            return {"phase": session.phase, "keep_working": session.keep_working_count}, github
        if decision == "limit":
            await self._block_needs_human(
                db, session, active,
                f"Arena tool-call limit: the task is not finished and all {session.keep_working_count} "
                "automatic continues were used",
            )
            return {"phase": session.phase, "limit": True}, github
        skipped = [e for e in session.events or [] if e.get("kind") == "tool_limit_skipped"]
        if not skipped or skipped[-1].get("detail") != decision:
            append_event(session, "tool_limit_skipped", decision)
        return None, github

    async def _report_on_page(self, session: ArenaSession) -> bool:
        try:
            return report_is_complete(str((await self.adapter.read_last_reply(session.profile_id)).get("text") or ""))
        except ArenaAgentError as exc:
            append_event(session, "read_reply_failed", exc.code)
            return False

    async def _transcript_mark(self, session: ArenaSession) -> str | None:
        try:
            latest = (await self.adapter.selector_test(session.profile_id)).get("latest_reply") or {}
        except ArenaAgentError as exc:
            append_event(session, "stall_check_failed", exc.code)
            return None
        return f"{latest.get('count')}:{latest.get('chars')}"

    @staticmethod
    def _mark_since(session: ArenaSession, reference: datetime) -> str | None:
        marks = [e for e in session.events or [] if e.get("kind") == "page_mark"
                 and _naive(datetime.fromisoformat(e["ts"])) >= reference]
        return marks[-1].get("detail") if marks else None

    async def _note_page_mark(self, session: ArenaSession, reference: datetime) -> None:
        """Halfway to a stall, remember the transcript. Stored on the session, so a restart keeps it."""
        if self._mark_since(session, reference) is None:
            mark = await self._transcript_mark(session)
            if mark:
                append_event(session, "page_mark", mark)

    async def _page_progressed(self, session: ArenaSession, reference: datetime) -> bool:
        """Read-only: did the transcript change since the halfway mark?"""
        earlier = self._mark_since(session, reference)
        return earlier is not None and (await self._transcript_mark(session)) not in {None, earlier}

    async def _close_feedback_once(self, session: ArenaSession) -> None:
        """A finished task: dismiss "Was this task successful?" without answering it."""
        if any(e.get("kind") == "feedback_closed" for e in session.events or []):
            return
        try:
            closed = await self.adapter.close_feedback(session.profile_id)
        except ArenaAgentError as exc:
            append_event(session, "feedback_close_failed", exc.code)
            return
        if closed.get("closed"):
            append_event(session, "feedback_closed")

    async def _secure_pull_request(self, db: AsyncSession, session: ArenaSession, parent: Task, project: Project | None,
                                   page: str) -> dict[str, Any] | None:
        """The agent finished without a usable PR: publish its work and open the PR from AATM."""
        slug = repo_slug(project)
        if not slug or not project:
            return None
        try:
            finished = page == "pr_ready" or reply_has_final_report(
                str((await self.adapter.read_last_reply(session.profile_id)).get("text") or ""))
            if not finished:
                return None
            path = str(((await self.adapter.selector_test(session.profile_id)).get("page") or {}).get("path") or "")
        except ArenaAgentError as exc:
            append_event(session, "secure_pr_failed", exc.code)
            return None
        suffix = path.rstrip("/").rsplit("/", 1)[-1][-8:]
        cwd = project.repo_path
        listed = await git_workflow_manager.list_arena_branches(repo_slug=slug, cwd=cwd)
        branch = next((b["name"] for b in listed.get("branches", []) if suffix and b["name"].startswith(f"arena/{suffix}-")), None)
        if not branch:
            return None
        base = project_base_branch(project)
        ahead = await git_workflow_manager.commits_ahead(repo_slug=slug, base=base, head=branch, cwd=cwd)
        if not ahead.get("ok"):
            return None
        if ahead["ahead"] <= 0:
            clicks = [e for e in session.events or [] if e.get("kind") == "arena_create_pr"]
            if clicks:
                clicked_at = datetime.fromisoformat(clicks[-1]["ts"])
                waited = datetime.now(timezone.utc) - clicked_at
                return {"waiting": "arena_push"} if waited < timedelta(minutes=CREATE_PR_WAIT_MINUTES) else None
            try:
                await self.adapter.create_pr(session.profile_id, repo=slug, branch=branch)
            except ArenaAgentError as exc:
                append_event(session, "arena_create_pr_failed", exc.code)
                return None
            append_event(session, "arena_create_pr", branch)
            return {"waiting": "arena_push"}
        result = await git_workflow_manager.ensure_task_pull_request(
            repo_slug=slug, task_id=parent.id, title=parent.title, branch=branch, base=base, cwd=cwd)
        if not result.get("ok"):
            append_event(session, "pr_ensure_failed", str(result.get("code")))
            return None
        append_event(session, "pr_ensured", ", ".join(result.get("actions") or []) or "already correct")
        await self._poll_github(db, session, parent, project)
        return {"pr_ensured": True}

    async def _reports_github_blocked(self, session: ArenaSession) -> bool:
        try:
            reply = await self.adapter.read_last_reply(session.profile_id)
        except ArenaAgentError as exc:
            append_event(session, "read_reply_failed", f"{exc.code}: {exc.message}")
            return False
        return reports_github_blocked(str(reply.get("text") or ""))

    async def _follow_up(self, db: AsyncSession, session: ArenaSession, kind: str, text: str) -> bool:
        session = await db.get(ArenaSession, session.id, populate_existing=True)
        if session is None:
            return False
        parent = await db.get(Task, session.task_id, populate_existing=True) if session.task_id else None
        repo = repo_slug(await self._project(db, parent)) if parent else None
        session, active = await self._refresh_send_target(db, session, kind=kind)
        if session is None or active is None:
            return False
        failures = self._chat_state()["failures"]
        try:
            await self.adapter.submit(session.profile_id, text, repo=repo)
        except ArenaAgentError as exc:
            append_event(session, f"{kind}_failed", f"{exc.code}: {exc.message}")
            failures[session.id] = failures.get(session.id, 0) + 1
            active = await db.get(Task, session.active_task_id) if session.active_task_id else parent
            if active is not None and (exc.code == "needs_human" or exc.code in REPO_SAFETY_CODES):
                # Retrying cannot help: the window shows another chat or repository.
                failures.pop(session.id, None)
                await self._block_needs_human(db, session, active, f"follow-up not sent: {exc.message}. Open the task's chat "
                                                                   "in this window, then press Resume")
            elif active is not None and failures[session.id] >= FOLLOW_UP_FAILURES_BEFORE_HUMAN:
                failures.pop(session.id, None)
                await self._block_needs_human(db, session, active, f"follow-up failed {FOLLOW_UP_FAILURES_BEFORE_HUMAN} times in a row "
                                                                   f"({exc.code}: {exc.message})")
            else:
                await db.commit()
            return False
        failures.pop(session.id, None)
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
