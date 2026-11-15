from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.employees.catalog import ensure_employee_workspace
from app.models import Agent, AgentMessage, ApprovalRequest, AuditLog, ChatMessage, Notification, Task

PRIORITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


class TaskScheduler:
    """Dependency-aware DAG scheduler, lock manager, retry engine & Manager coordinator."""

    @staticmethod
    def _has_cycle(task_id: str, new_deps: list[str], all_tasks: dict[str, Task]) -> bool:
        visited: set[str] = set()
        stack = list(new_deps)
        while stack:
            cur = stack.pop()
            if cur == task_id:
                return True
            if cur in visited:
                continue
            visited.add(cur)
            t = all_tasks.get(cur)
            if t and t.dependencies:
                stack.extend(t.dependencies)
        return False

    async def validate_dependencies(self, db: AsyncSession, task_id: str | None, deps: list[str]) -> None:
        if not deps:
            return
        res = await db.execute(select(Task))
        all_tasks = {t.id: t for t in res.scalars().all()}
        for dep_id in deps:
            if dep_id not in all_tasks:
                raise ValueError(f"Dependency task '{dep_id}' does not exist.")
        if task_id and self._has_cycle(task_id, deps, all_tasks):
            raise ValueError("Circular dependency detected in task graph.")

    async def dependencies_satisfied(self, db: AsyncSession, task: Task) -> bool:
        if not task.dependencies:
            return True
        res = await db.execute(select(Task).where(Task.id.in_(task.dependencies)))
        deps = list(res.scalars().all())
        if len(deps) != len(task.dependencies):
            return False
        return all(d.status == "completed" for d in deps)

    async def acquire_lock(self, db: AsyncSession, task: Task, worker_id: str) -> bool:
        if task.lock_owner and task.lock_owner != worker_id:
            return False
        task.lock_owner = worker_id
        task.locked_at = utcnow()
        await db.commit()
        return True

    async def release_lock(self, db: AsyncSession, task: Task) -> None:
        task.lock_owner = None
        task.locked_at = None
        await db.commit()

    async def run_orchestration_cycle(self, db: AsyncSession) -> dict[str, Any]:
        """Assign ready queued tasks to idle enabled workers respecting dependencies, mode, skills & concurrency."""
        settings = get_settings()
        agents_res = await db.execute(
            select(Agent).where(
                Agent.role == "worker",
                Agent.is_enabled.is_(True),
                Agent.is_emergency_stopped.is_(False),
            )
        )
        workers = list(agents_res.scalars().all())
        idle_workers = [w for w in workers if w.status == "idle"]
        working_count = sum(1 for w in workers if w.status == "working")

        tasks_res = await db.execute(select(Task).where(Task.status.in_(["queued", "assigned", "in_progress"])))
        tasks = list(tasks_res.scalars().all())
        queued = [t for t in tasks if t.status == "queued"]
        queued.sort(key=lambda t: (-PRIORITY_WEIGHT.get(t.priority, 2), t.created_at))

        assigned: list[dict[str, str]] = []
        available_slots = max(0, settings.MAX_CONCURRENT_WORKERS - working_count)

        for task in queued:
            if available_slots <= 0 or not idle_workers:
                break
            if not await self.dependencies_satisfied(db, task):
                continue

            chosen: Agent | None = None
            if task.execution_mode == "manual":
                if not task.assigned_agent_id:
                    continue
                chosen = next((w for w in idle_workers if w.id == task.assigned_agent_id), None)
            else:
                if task.assigned_agent_id:
                    chosen = next((w for w in idle_workers if w.id == task.assigned_agent_id), None)
                if not chosen:
                    # Automatic selection: score by division match, performance & lower failures
                    def _score(w: Agent) -> tuple[int, float, int]:
                        div_match = 1 if task.division_code in (w.divisions or []) else 0
                        return (div_match, w.performance_score, -w.tasks_failed)

                    chosen = max(idle_workers, key=_score)

            if not chosen or not await self.acquire_lock(db, task, chosen.id):
                continue

            ws = ensure_employee_workspace(chosen.id)
            task.assigned_agent_id = chosen.id
            task.status = "in_progress"
            task.started_at = task.started_at or utcnow()
            task.checkpoints = list(task.checkpoints or []) + [
                {"ts": utcnow().isoformat(), "worker": chosen.id, "mode": task.execution_mode, "note": "Dispatched by TaskScheduler"}
            ]

            chosen.status = "working"
            chosen.current_task_id = task.id
            chosen.last_heartbeat = utcnow()

            # Deliver structured AATM Agent Message automatically
            db.add(
                AgentMessage(
                    task_id=task.id,
                    project_id=task.project_id,
                    sender_id="manager-0",
                    recipient_id=chosen.id,
                    channel_type="task",
                    instructions=f"[{task.execution_mode.upper()}] {task.title}: {task.description}",
                    priority=task.priority,
                    required_tools=["workspace_fs", "python_runner"],
                    workspace_info={"workspace_path": str(ws), "branch": chosen.git_branch},
                    expected_deliverables=task.expected_deliverables or [],
                    permissions=chosen.permissions or [],
                    execution_status="running",
                    delivery_status="acknowledged",
                    acked_at=utcnow(),
                )
            )

            idle_workers.remove(chosen)
            available_slots -= 1
            assigned.append({"task_id": task.id, "title": task.title, "worker_id": chosen.id})

            db.add(
                AuditLog(
                    actor="orchestrator",
                    action="task.dispatched",
                    resource_type="task",
                    resource_id=task.id,
                    details={"worker_id": chosen.id, "division": task.division_code, "mode": task.execution_mode},
                )
            )

        await db.commit()
        if assigned:
            await event_bus.emit("orchestration.cycle", {"dispatched": assigned})
        return {"dispatched": assigned, "working_workers": working_count + len(assigned), "idle_workers": len(idle_workers)}

    async def complete_task(self, db: AsyncSession, task: Task, summary: str = "Completed and verified") -> Task:
        task.status = "completed"
        task.progress_percent = 100
        task.result_summary = summary
        task.completed_at = utcnow()
        task.lock_owner = None
        task.locked_at = None

        if task.assigned_agent_id:
            worker = await db.get(Agent, task.assigned_agent_id)
            if worker:
                worker.tasks_completed += 1
                worker.status = "idle"
                worker.current_task_id = None
                worker.last_heartbeat = utcnow()

        db.add(
            AuditLog(
                actor=task.assigned_agent_id or "orchestrator",
                action="task.completed",
                resource_type="task",
                resource_id=task.id,
                details={"summary": summary},
            )
        )
        await db.commit()
        await db.refresh(task)
        await event_bus.emit("task.updated", {"task_id": task.id, "status": "completed"})
        return task

    async def fail_and_retry_or_escalate(self, db: AsyncSession, task: Task, error_msg: str) -> Task:
        task.error_message = error_msg
        task.retry_count += 1
        task.lock_owner = None
        task.locked_at = None

        if task.assigned_agent_id:
            worker = await db.get(Agent, task.assigned_agent_id)
            if worker:
                worker.tasks_failed += 1
                worker.status = "idle"
                worker.current_task_id = None
                worker.last_heartbeat = utcnow()

        if task.retry_count <= task.max_retries:
            task.status = "queued"
            task.checkpoints = list(task.checkpoints or []) + [
                {"ts": utcnow().isoformat(), "note": f"Retry #{task.retry_count} scheduled after error: {error_msg}"}
            ]
        else:
            task.status = "failed"
            db.add(
                Notification(
                    category="task",
                    severity="error",
                    title=f"Task Failed After {task.max_retries} Retries",
                    message=f"{task.title}: {error_msg}",
                    link="/tasks",
                )
            )
        await db.commit()
        await db.refresh(task)
        await event_bus.emit("task.updated", {"task_id": task.id, "status": task.status})
        return task

    async def detect_and_recover_stalled_tasks(self, db: AsyncSession) -> list[str]:
        settings = get_settings()
        cutoff = utcnow() - timedelta(minutes=settings.STALLED_TASK_MINUTES)
        res = await db.execute(
            select(Task).where(Task.status == "in_progress", Task.updated_at < cutoff)
        )
        recovered: list[str] = []
        for task in res.scalars().all():
            await self.fail_and_retry_or_escalate(db, task, "Stalled task timeout detected; auto-requeued by self-healing engine.")
            recovered.append(task.id)
        return recovered

    async def handle_manager_directive(self, db: AsyncSession, project_id: str | None, content: str) -> ChatMessage:
        """Process a user command in Manager Chat, create tasks/approvals if requested, and respond."""
        lower = content.lower()
        actions_taken: list[str] = []

        # High-risk keywords trigger Approval Center rather than silent execution
        risky_map = [
            ("merge main", "merge", "Merge changes into protected branch `main`"),
            ("deploy prod", "deploy", "Execute production deployment"),
            ("increase budget", "spend", "Increase AI provider spend limit"),
        ]
        for trigger, action_type, title in risky_map:
            if trigger in lower:
                app = ApprovalRequest(
                    project_id=project_id,
                    action_type=action_type,
                    title=title,
                    description=f"Requested via Manager Chat directive: {content}",
                    requested_by="manager-0",
                    status="pending",
                )
                db.add(app)
                actions_taken.append(f"Created Approval Request ({action_type}) awaiting user authorization.")

        if any(k in lower for k in ("create task", "assign", "build", "implement", "fix")) and project_id:
            div = "D02" if "api" in lower or "backend" in lower else "D03" if "ui" in lower or "frontend" in lower else "D05" if "provider" in lower or "browser" in lower else "D11"
            owner = "worker-1" if div == "D02" else "worker-2" if div == "D03" else "worker-3" if div == "D05" else "worker-4"
            new_task = Task(
                project_id=project_id,
                division_code=div,
                title=content[:120],
                description=f"Auto-planned by Chief Manager from directive: {content}",
                priority="high",
                status="queued",
                assigned_agent_id=owner,
            )
            db.add(new_task)
            await db.flush()
            actions_taken.append(f"Planned task '{new_task.title}' in {div} assigned to {owner}.")

        if "orchestrate" in lower or "dispatch" in lower or "run cycle" in lower:
            cycle = await self.run_orchestration_cycle(db)
            actions_taken.append(f"Orchestration cycle dispatched {len(cycle['dispatched'])} task(s).")

        agents_res = await db.execute(select(Agent))
        agents = list(agents_res.scalars().all())
        working = sum(1 for a in agents if a.role == "worker" and a.status == "working")
        idle = sum(1 for a in agents if a.role == "worker" and a.status == "idle")

        reply_parts = [
            f"Manager Directive Acknowledged. Team telemetry: {working} worker(s) active, {idle} idle."
        ]
        if actions_taken:
            reply_parts.append("Actions executed:\n- " + "\n- ".join(actions_taken))
        else:
            reply_parts.append("Workers are synchronized across the 12 divisions. Say 'create task ...', 'orchestrate', or 'run cycle' to dispatch work directly.")

        msg = ChatMessage(
            project_id=project_id,
            sender_type="manager",
            sender_id="manager-0",
            recipient_id="user",
            content="\n".join(reply_parts),
            metadata_json={"actions": actions_taken},
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        await event_bus.emit("chat.message", {"id": msg.id, "sender": "manager-0", "content": msg.content})
        return msg


task_scheduler = TaskScheduler()
