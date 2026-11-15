import shutil
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.manager import browser_manager
from app.core.events import event_bus
from app.db.base import utcnow
from app.employees.catalog import ensure_employee_workspace
from app.models import Agent, AgentMessage, AuditLog, BrowserProfile, Notification, Task

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

        def score(cand: Agent) -> tuple[int, int, float]:
            skill_overlap = len(failing_skills & set(cand.skills or []))
            div_overlap = len(failing_divs & set(cand.divisions or []))
            idle_bonus = 2 if cand.status == "idle" else 0
            return (skill_overlap + div_overlap + idle_bonus, -cand.tasks_failed, cand.performance_score)

        replacement = max(candidates, key=score)

        # Transfer active/assigned tasks & workspace deliverables
        tasks_res = await db.execute(
            select(Task).where(
                Task.assigned_agent_id == failing_agent_id,
                Task.status.in_(["queued", "assigned", "in_progress", "blocked"]),
            )
        )
        transferred_tasks: list[str] = []
        src_ws = ensure_employee_workspace(failing_agent_id)
        dst_ws = ensure_employee_workspace(replacement.id)
        transferred_files: list[str] = []

        if src_ws.exists():
            handoff_dir = dst_ws / f"handoff_from_{failing_agent_id}"
            handoff_dir.mkdir(parents=True, exist_ok=True)
            for item in src_ws.iterdir():
                if item.is_file():
                    shutil.copy2(item, handoff_dir / item.name)
                    transferred_files.append(item.name)

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

        failing.status = "offline"
        failing.current_task_id = None
        replacement.last_heartbeat = utcnow()
        if transferred_tasks:
            replacement.current_task_id = transferred_tasks[0]

        db.add(
            AuditLog(
                actor=actor,
                action="employee.replaced",
                resource_type="agent",
                resource_id=failing_agent_id,
                details={
                    "replacement_id": replacement.id,
                    "transferred_tasks": transferred_tasks,
                    "transferred_files": transferred_files,
                    "reason": reason,
                },
                severity="warning",
            )
        )
        db.add(
            Notification(
                category="replacement",
                severity="warning",
                title=f"Smart Replacement: {failing.name} -> {replacement.name}",
                message=f"Transferred {len(transferred_tasks)} task(s) and {len(transferred_files)} workspace file(s).",
                link="/agents",
            )
        )
        await db.commit()
        await event_bus.emit("employee.replaced", {"from": failing_agent_id, "to": replacement.id, "tasks": transferred_tasks})
        return {
            "replaced_employee": failing_agent_id,
            "replacement_employee": replacement.id,
            "transferred_tasks": transferred_tasks,
            "transferred_files": transferred_files,
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
