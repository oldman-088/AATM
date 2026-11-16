import inspect
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.manager import browser_manager
from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.employees.catalog import allocate_account_and_session, ensure_employee_workspace
from app.git_workflows.manager import git_workflow_manager
from app.models import (
    Agent,
    AgentMessage,
    AIProvider,
    AuditLog,
    BrowserProfile,
    Notification,
    ProviderAccount,
    ProviderSession,
    Task,
)
from app.providers.adapters import ArenaLocalBridgeAdapter
from app.runtime.sandbox import WorkspaceSandboxError, is_sensitive_workspace_path, resolve_contained_path

logger = logging.getLogger(__name__)

MAX_HANDOFF_FILES = 1_000
MAX_HANDOFF_FILE_BYTES = 5 * 1024 * 1024
MAX_HANDOFF_TOTAL_BYTES = 25 * 1024 * 1024


def copy_workspace_handoff(source_agent_id: str, recipient_agent_id: str) -> tuple[str | None, list[str]]:
    """Copy bounded, non-sensitive regular files into a recipient's isolated handoff directory."""
    if source_agent_id == recipient_agent_id:
        return None, []

    settings = get_settings()
    workspace_root = Path(settings.WORKSPACES_DIR).resolve()
    id_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

    def _workspace(agent_id: str) -> Path:
        if not isinstance(agent_id, str) or not id_pattern.fullmatch(agent_id):
            raise WorkspaceSandboxError("Employee workspace identifiers must be simple, non-empty path components.")
        requested = workspace_root / agent_id
        if requested.is_symlink():
            raise WorkspaceSandboxError("Employee workspace roots may not be symbolic links.")
        expected = resolve_contained_path(workspace_root, agent_id)
        if expected.parent != workspace_root:
            raise WorkspaceSandboxError("Employee workspace must be a direct child of the configured workspace root.")
        actual = ensure_employee_workspace(agent_id)
        if actual != expected:
            raise WorkspaceSandboxError("Employee workspace resolved outside its configured workspace root.")
        return actual

    source_root = _workspace(source_agent_id)
    recipient_root = _workspace(recipient_agent_id)
    handoff_name = f"handoff_from_{source_agent_id}"
    handoff_candidate = recipient_root / handoff_name
    if handoff_candidate.is_symlink():
        raise WorkspaceSandboxError("A handoff directory may not be a symbolic link.")
    handoff_candidate.mkdir(parents=True, exist_ok=True)
    handoff_root = resolve_contained_path(recipient_root, handoff_name)

    copied_files: list[str] = []
    copied_bytes = 0
    for current_dir, dirnames, filenames in os.walk(source_root, topdown=True, followlinks=False):
        current = Path(current_dir)
        safe_dirs: list[str] = []
        for name in sorted(dirnames):
            directory = current / name
            relative_dir = directory.relative_to(source_root).as_posix()
            if directory.is_symlink() or is_sensitive_workspace_path(relative_dir):
                continue
            safe_dirs.append(name)
        dirnames[:] = safe_dirs

        for name in sorted(filenames):
            source = current / name
            relative = source.relative_to(source_root).as_posix()
            if source.is_symlink() or is_sensitive_workspace_path(relative) or not source.is_file():
                continue
            try:
                contained_source = resolve_contained_path(source_root, relative)
                file_size = contained_source.stat().st_size
            except (OSError, WorkspaceSandboxError):
                continue
            if file_size > MAX_HANDOFF_FILE_BYTES or copied_bytes + file_size > MAX_HANDOFF_TOTAL_BYTES:
                continue

            cursor = handoff_root
            has_symlink = False
            for part in Path(relative).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    has_symlink = True
                    break
            if has_symlink:
                continue
            try:
                destination = resolve_contained_path(handoff_root, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                    continue
                shutil.copyfile(contained_source, destination)
            except (OSError, WorkspaceSandboxError):
                continue

            copied_files.append(relative)
            copied_bytes += file_size
            if len(copied_files) >= MAX_HANDOFF_FILES:
                return handoff_name, copied_files

    return handoff_name, copied_files


# Marker written into `Task.error_message` so tasks halted by an Emergency Stop can be
# identified later and resumed. Without it a globally stopped task stays "blocked"
# forever: the scheduler only dispatches "queued" tasks and the stalled-task sweep only
# inspects "in_progress" tasks.
EMERGENCY_STOP_MARKER = "Emergency stop"


class ReplacementAndEmergencyService:
    """Smart Employee Replacement & Scoped/Global Emergency Stop."""

    async def replace_employee(
        self,
        db: AsyncSession,
        failing_agent_id: str,
        reason: str = "Unavailable or repeated failures",
        actor: str = "manager-0",
    ) -> dict[str, Any]:
        failing = await db.get(Agent, failing_agent_id)
        if not failing:
            raise ValueError(f"Employee '{failing_agent_id}' not found.")

        candidates_res = await db.execute(
            select(Agent).where(
                Agent.id != failing_agent_id,
                Agent.is_enabled.is_(True),
                Agent.is_emergency_stopped.is_(False),
                Agent.status.in_(["idle", "working"]),
            )
        )
        candidates = list(candidates_res.scalars().all())
        if not candidates:
            raise ValueError("No healthy replacement employee available.")

        failing_skills = set(failing.skills or [])
        failing_divs = set(failing.divisions or [])

        def score(cand: Agent) -> tuple[int, int]:
            skill_overlap = len(failing_skills & set(cand.skills or []))
            div_overlap = len(failing_divs & set(cand.divisions or []))
            idle_bonus = 2 if cand.status == "idle" else 0
            # performance_score is a legacy default, not measured telemetry.
            return (skill_overlap + div_overlap + idle_bonus, -cand.tasks_failed)

        replacement = max(candidates, key=score)

        # Transfer active/assigned tasks & workspace deliverables
        tasks_res = await db.execute(
            select(Task).where(
                Task.assigned_agent_id == failing_agent_id,
                Task.status.in_(["queued", "assigned", "in_progress", "blocked"]),
            )
        )
        transferred_tasks: list[str] = []
        _, transferred_files = copy_workspace_handoff(failing_agent_id, replacement.id)
        dst_ws = ensure_employee_workspace(replacement.id)

        for task in tasks_res.scalars().all():
            task.assigned_agent_id = replacement.id
            task.lock_owner = replacement.id
            task.checkpoints = list(task.checkpoints or []) + [
                {
                    "ts": utcnow().isoformat(),
                    "event": "smart_replacement",
                    "from": failing_agent_id,
                    "to": replacement.id,
                    "reason": reason,
                    "transferred_files": transferred_files,
                }
            ]
            transferred_tasks.append(task.id)
            db.add(
                AgentMessage(
                    task_id=task.id,
                    project_id=task.project_id,
                    sender_id="manager-0",
                    recipient_id=replacement.id,
                    channel_type="task",
                    instructions=f"SMART REPLACEMENT HANDOFF from {failing_agent_id}: {task.title}. Reason: {reason}. Files copied to handoff_from_{failing_agent_id}/.",
                    priority=task.priority,
                    workspace_info={"workspace_path": str(dst_ws), "handoff_files": transferred_files},
                    execution_status="delivered",
                    delivery_status="delivered",
                )
            )

        failing_name = failing.name
        replacement_id = replacement.id
        replacement_name = replacement.name

        failing.status = "offline"
        failing.current_task_id = None
        replacement.last_heartbeat = utcnow()
        if transferred_tasks:
            replacement.current_task_id = transferred_tasks[0]

        replacement_session_allocated = await self._ensure_replacement_session(db, replacement)
        replacement_session_id = replacement.provider_session_id

        bridge_cleanup = await self._cleanup_bridge_session(db, failing)
        stopped_browsers = await self._stop_employee_browsers(db, failing, actor=actor)
        worktree_cleanup = await self._cleanup_employee_worktree(failing_agent_id)

        db.add(
            AuditLog(
                actor=actor,
                action="employee.replaced",
                resource_type="agent",
                resource_id=failing_agent_id,
                details={
                    "replacement_id": replacement_id,
                    "replacement_session_id": replacement_session_id,
                    "replacement_session_allocated": replacement_session_allocated,
                    "transferred_tasks": transferred_tasks,
                    "transferred_files": transferred_files,
                    "reason": reason,
                    "bridge_session_cleanup": bridge_cleanup,
                    "stopped_browsers": stopped_browsers,
                    "worktree_cleanup": worktree_cleanup,
                },
                severity="warning",
            )
        )
        browser_note = f" Stopped {len(stopped_browsers)} browser profile(s)." if stopped_browsers else ""
        db.add(
            Notification(
                category="replacement",
                severity="warning",
                title=f"Smart Replacement: {failing_name} -> {replacement_name}",
                message=(
                    f"Transferred {len(transferred_tasks)} task(s) and {len(transferred_files)} workspace file(s)."
                    + browser_note
                ),
                link="/agents",
            )
        )
        await db.commit()
        await event_bus.emit(
            "employee.replaced",
            {
                "from": failing_agent_id,
                "to": replacement_id,
                "replacement_session_id": replacement_session_id,
                "tasks": transferred_tasks,
                "bridge_session_cleanup": bridge_cleanup,
                "stopped_browsers": stopped_browsers,
                "worktree_cleanup": worktree_cleanup,
            },
        )
        return {
            "replaced_employee": failing_agent_id,
            "replacement_employee": replacement_id,
            "replacement_session_id": replacement_session_id,
            "replacement_session_allocated": replacement_session_allocated,
            "transferred_tasks": transferred_tasks,
            "transferred_files": transferred_files,
            "bridge_session_cleanup": bridge_cleanup,
            "stopped_browsers": stopped_browsers,
            "worktree_cleanup": worktree_cleanup,
        }

    async def _ensure_replacement_session(
        self,
        db: AsyncSession,
        replacement: Agent,
    ) -> dict[str, Any] | None:
        """Ensure the replacement employee has an active ProviderSession, allocating one if missing or closed."""
        existing_sess: ProviderSession | None = None
        if replacement.provider_session_id:
            existing_sess = await db.get(ProviderSession, replacement.provider_session_id)
        if existing_sess is not None and existing_sess.status != "closed":
            return None

        provider_id = replacement.provider_id or "arena-agent"
        acct_id, sess_id = await allocate_account_and_session(
            db,
            agent_id=replacement.id,
            provider_id=provider_id,
            preferred_account_id=replacement.provider_account_id,
            browser_profile_id=replacement.browser_profile_id,
        )
        replacement.provider_account_id = acct_id
        replacement.provider_session_id = sess_id
        return {"account_id": acct_id, "session_id": sess_id, "provider_id": provider_id}

    @staticmethod
    async def _cleanup_employee_worktree(failing_agent_id: str) -> dict[str, Any]:
        """Best-effort cleanup of any stale git worktree belonging to the replaced employee."""
        try:
            res = git_workflow_manager.remove_agent_worktree(failing_agent_id)
            if inspect.isawaitable(res):
                res = await res
            return dict(res) if isinstance(res, dict) else {"ok": True, "removed": True, "agent_id": failing_agent_id}
        except Exception as exc:  # noqa: BLE001 - best-effort worktree hygiene must never abort replacement
            logger.warning(
                "Best-effort worktree cleanup failed for agent=%s: %s",
                failing_agent_id,
                type(exc).__name__,
            )
            return {
                "ok": False,
                "removed": False,
                "agent_id": failing_agent_id,
                "error": f"Worktree cleanup error: {type(exc).__name__}",
            }

    async def _stop_employee_browsers(
        self,
        db: AsyncSession,
        failing: Agent,
        *,
        actor: str,
    ) -> list[str]:
        """Stop any running BrowserProfile owned or locked by the replaced employee to release .aatm.lock and contexts."""
        ownership_filters = [
            BrowserProfile.agent_id == failing.id,
            BrowserProfile.lock_owner == failing.id,
        ]
        if failing.browser_profile_id:
            ownership_filters.append(BrowserProfile.id == failing.browser_profile_id)

        bp_res = await db.execute(
            select(BrowserProfile).where(
                BrowserProfile.status == "running",
                or_(*ownership_filters),
            )
        )
        running_profiles = list(bp_res.scalars().all())
        stopped_ids: list[str] = []
        for bp in running_profiles:
            bp_id = bp.id
            try:
                await browser_manager.stop_profile(db, bp, actor=actor)
                stopped_ids.append(bp_id)
            except Exception as exc:  # noqa: BLE001 - best-effort browser lock cleanup must never abort replacement
                logger.warning(
                    "Best-effort browser stop failed for agent=%s profile=%s: %s",
                    failing.id,
                    bp_id,
                    type(exc).__name__,
                )
        return stopped_ids

    async def _cleanup_bridge_session(
        self,
        db: AsyncSession,
        failing: Agent,
    ) -> dict[str, Any] | None:
        """Best-effort DELETE /v1/sessions/{old_session_id} if the replaced employee used arena_local_bridge."""
        old_session_id = failing.provider_session_id
        old_account_id = failing.provider_account_id
        sess_row: ProviderSession | None = None

        if old_session_id:
            sess_row = await db.get(ProviderSession, old_session_id)
        else:
            sess_res = await db.execute(
                select(ProviderSession)
                .where(ProviderSession.agent_id == failing.id, ProviderSession.status == "active")
                .order_by(ProviderSession.created_at.desc())
                .limit(1)
            )
            sess_row = sess_res.scalar_one_or_none()
            if sess_row:
                old_session_id = sess_row.id

        if sess_row and not old_account_id:
            old_account_id = sess_row.account_id

        bridge_provider: AIProvider | None = None
        if failing.provider_id:
            cand = await db.get(AIProvider, failing.provider_id)
            if cand and cand.provider_type == "arena_local_bridge":
                bridge_provider = cand

        if bridge_provider is None and old_account_id:
            acct = await db.get(ProviderAccount, old_account_id)
            if acct and acct.provider_id:
                cand = await db.get(AIProvider, acct.provider_id)
                if cand and cand.provider_type == "arena_local_bridge":
                    bridge_provider = cand

        if bridge_provider is None or not old_session_id:
            return None

        if sess_row:
            sess_row.status = "closed"

        adapter = ArenaLocalBridgeAdapter(
            provider_id=bridge_provider.id,
            endpoint_url=bridge_provider.endpoint_url,
            credential_ref=bridge_provider.credential_ref,
            default_model=bridge_provider.default_model,
        )
        try:
            return await adapter.delete_session(old_session_id, account_id=old_account_id)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup must never abort replacement
            logger.warning(
                "Best-effort bridge session cleanup failed for agent=%s session=%s: %s",
                failing.id,
                old_session_id,
                type(exc).__name__,
            )
            return {
                "deleted": False,
                "session_id": old_session_id,
                "account_id": old_account_id,
                "status": "error",
                "bridge_reachable": False,
                "error": f"Bridge cleanup error: {type(exc).__name__}",
            }

    async def emergency_stop(
        self,
        db: AsyncSession,
        scope: str,  # global | employee | task | browser
        target_id: str | None = None,
        actor: str = "admin@aatm.local",
        reason: str = "Manual Emergency Stop",
    ) -> dict[str, Any]:
        stopped = {"agents": 0, "tasks": 0, "browsers": 0, "scope": scope}

        if scope in ("global", "employee"):
            stmt = select(Agent) if scope == "global" else select(Agent).where(Agent.id == target_id)
            for ag in (await db.execute(stmt)).scalars().all():
                ag.is_emergency_stopped = True
                ag.status = "paused"
                ag.current_task_id = None
                stopped["agents"] += 1

        # A halted employee must not leave its in-flight task running: the task would stay
        # "in_progress" with a lock owned by a stopped agent and could never be re-dispatched.
        if scope == "employee" and target_id:
            task_stmt = select(Task).where(
                Task.assigned_agent_id == target_id,
                Task.status.in_(["in_progress", "assigned"]),
            )
            for t in (await db.execute(task_stmt)).scalars().all():
                self._halt_task(t, scope, reason)
                stopped["tasks"] += 1
        elif scope in ("global", "task"):
            task_stmt = select(Task).where(Task.status.in_(["in_progress", "assigned"]))
            if scope == "task":
                task_stmt = select(Task).where(Task.id == target_id)
            for t in (await db.execute(task_stmt)).scalars().all():
                if scope == "task":
                    t.status = "cancelled"
                    t.lock_owner = None
                    t.locked_at = None
                    t.error_message = f"{EMERGENCY_STOP_MARKER} ({scope}): {reason}"
                else:
                    self._halt_task(t, scope, reason)
                stopped["tasks"] += 1

        if scope in ("global", "browser"):
            bp_stmt = select(BrowserProfile).where(BrowserProfile.status == "running")
            if scope == "browser":
                bp_stmt = select(BrowserProfile).where(BrowserProfile.id == target_id)
            for bp in (await db.execute(bp_stmt)).scalars().all():
                await browser_manager.stop_profile(db, bp, actor=actor)
                stopped["browsers"] += 1

        db.add(
            AuditLog(
                actor=actor,
                action="security.emergency_stop",
                resource_type=scope,
                resource_id=target_id,
                details={"reason": reason, **stopped},
                severity="critical",
            )
        )
        db.add(
            Notification(
                category="security",
                severity="error",
                title=f"EMERGENCY STOP ACTIVATED ({scope.upper()})",
                message=f"{reason} — Halted {stopped['agents']} agent(s), {stopped['tasks']} task(s), {stopped['browsers']} browser(s).",
                link="/",
            )
        )
        await db.commit()
        await event_bus.emit("security.emergency_stop", stopped)
        return stopped

    @staticmethod
    def _halt_task(task: Task, scope: str, reason: str) -> None:
        """Block a task and release its lock, marking it as resumable after the stop."""
        task.status = "blocked"
        task.lock_owner = None
        task.locked_at = None
        task.error_message = f"{EMERGENCY_STOP_MARKER} ({scope}): {reason}"
        task.checkpoints = list(task.checkpoints or []) + [
            {
                "ts": utcnow().isoformat(),
                "event": "emergency_stop",
                "scope": scope,
                "reason": reason,
            }
        ]

    async def resume_emergency_stopped_tasks(
        self,
        db: AsyncSession,
        actor: str = "admin@aatm.local",
    ) -> list[str]:
        """Requeue every task that was blocked by an Emergency Stop so work can resume."""
        res = await db.execute(select(Task).where(Task.status == "blocked"))
        resumed: list[str] = []
        for task in res.scalars().all():
            if not (task.error_message or "").startswith(EMERGENCY_STOP_MARKER):
                continue
            task.status = "queued"
            task.lock_owner = None
            task.locked_at = None
            task.error_message = None
            task.checkpoints = list(task.checkpoints or []) + [
                {"ts": utcnow().isoformat(), "event": "emergency_stop_cleared", "actor": actor}
            ]
            resumed.append(task.id)
        if resumed:
            db.add(
                AuditLog(
                    actor=actor,
                    action="security.emergency_resume",
                    resource_type="task",
                    details={"requeued_tasks": resumed},
                    severity="warning",
                )
            )
            await db.commit()
        return resumed

    async def clear_emergency_stop(self, db: AsyncSession, actor: str = "admin@aatm.local") -> dict[str, int]:
        count = 0
        for ag in (await db.execute(select(Agent).where(Agent.is_emergency_stopped.is_(True)))).scalars().all():
            ag.is_emergency_stopped = False
            ag.status = "idle"
            ag.current_task_id = None
            count += 1
        resumed_tasks = await self.resume_emergency_stopped_tasks(db, actor=actor)
        db.add(
            AuditLog(
                actor=actor,
                action="security.emergency_clear",
                resource_type="global",
                details={"resumed": count, "requeued_tasks": resumed_tasks},
            )
        )
        await db.commit()
        return {"resumed_agents": count, "resumed_tasks": len(resumed_tasks)}


replacement_service = ReplacementAndEmergencyService()
