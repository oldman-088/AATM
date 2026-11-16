import asyncio
import json
import logging
import re
import time
from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.employees.catalog import ensure_employee_workspace
from app.models import (
    Agent,
    AgentMessage,
    ApprovalRequest,
    AuditLog,
    ChatMessage,
    Notification,
    Project,
    Task,
)
from app.providers.base import ProviderRequest, ProviderResponse
from app.providers.router import ProviderRouter, get_model_configuration_status
from app.providers.router import provider_router as shared_provider_router
from app.runtime.temporal import to_naive_utc
from app.schemas.manager import ManagerPlan

logger = logging.getLogger(__name__)

DEFAULT_SESSION_RECOVERY_INTERVAL_SECONDS = 60.0

PRIORITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}

# Identifier of the co-located execution node. Tasks pinned to any other device are
# executed by that device's remote worker daemon, never by the local scheduler.
LOCAL_DEVICE_ID = "device-local"

# Branches that must never be merged into without an explicit Approval Center decision.
PROTECTED_BRANCH_NAMES = ("main", "master", "production", "prod", "release")

# High-risk Manager Chat directives must be routed to the Approval Center instead of
# being executed silently. The patterns are deliberately tolerant of phrasing so that
# variants ("merge master into main", "push to production", "rm -rf the database")
# cannot slip past the governance gate.
HIGH_RISK_DIRECTIVE_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r"\b(merge|push|rebase|cherry[\s\-]?pick|fast[\s\-]?forward)\b[^.]{0,60}\b"
            r"(main|master|production|prod|release)\b",
            re.IGNORECASE,
        ),
        "merge",
        "Merge or push into a protected branch",
    ),
    (
        re.compile(
            r"\b(main|master|production|prod|release)\b[^.]{0,60}\b"
            r"(merge|push|rebase|cherry[\s\-]?pick)\b",
            re.IGNORECASE,
        ),
        "merge",
        "Merge or push into a protected branch",
    ),
    (
        re.compile(
            r"\b(deploy|roll\s?out|ship|publish|promote)\b[^.]{0,40}\b(prod|production|live|release)\b",
            re.IGNORECASE,
        ),
        "deploy",
        "Execute production deployment",
    ),
    (
        re.compile(
            r"\b(increase|raise|extend|expand|double)\b[^.]{0,30}\b(budget|spend|quota|limit)\b",
            re.IGNORECASE,
        ),
        "spend",
        "Increase AI provider spend limit",
    ),
    (
        re.compile(
            r"\b(budget|spend)\b[^.]{0,30}\b(increase|raise|extend|expand)\b",
            re.IGNORECASE,
        ),
        "spend",
        "Increase AI provider spend limit",
    ),
    (
        re.compile(
            r"\b(delete|drop|purge|wipe|destroy|truncate|rm\s+-rf)\b[^.]{0,40}\b"
            r"(data|database|db|tables?|records?|users?|all|production|prod|backups?|"
            r"workspaces?|repositor(y|ies))\b",
            re.IGNORECASE,
        ),
        "delete",
        "Delete or destroy critical data",
    ),
    (
        re.compile(
            r"\b(rotate|regenerate|reveal|show|print|exfiltrate|leak|change|update|reset)\b[^.]{0,40}\b"
            r"(credentials?|api[\s_\-]?keys?|secrets?|tokens?|passwords?|passphrases?|private[\s\-]key)\b",
            re.IGNORECASE,
        ),
        "credential",
        "Change or expose sensitive credentials",
    ),
]


class TaskScheduler:
    """Dependency-aware DAG scheduler, lock manager, retry engine & Manager coordinator."""

    def __init__(
        self,
        provider_router: ProviderRouter | None = None,
        session_recovery_interval_seconds: float = DEFAULT_SESSION_RECOVERY_INTERVAL_SECONDS,
    ) -> None:
        self.provider_router = provider_router or shared_provider_router
        self.session_recovery_interval_seconds = max(0.0, float(session_recovery_interval_seconds))
        self._last_session_recovery_at: float | None = None

    def reset_session_recovery_cooldown(self) -> None:
        """Clear the periodic throttle so the next cycle checks degraded sessions immediately."""
        self._last_session_recovery_at = None

    async def recover_sessions_if_needed(
        self,
        db: AsyncSession,
        *,
        force: bool = False,
        health_threshold: float = 50.0,
        actor: str = "scheduler",
    ) -> dict[str, Any]:
        """Periodic, best-effort hook delegating degraded ProviderSession recovery to ProviderRouter."""
        now = time.monotonic()
        if (
            not force
            and self._last_session_recovery_at is not None
            and (now - self._last_session_recovery_at) < self.session_recovery_interval_seconds
        ):
            return {"skipped": True, "recovered_count": 0, "sessions": []}

        self._last_session_recovery_at = now
        try:
            result = await self.provider_router.recover_degraded_sessions(
                db,
                health_threshold=health_threshold,
                actor=actor,
            )
            return {"skipped": False, **result}
        except Exception as exc:  # noqa: BLE001 - best-effort recovery must never block task dispatch
            logger.warning("Scheduler session recovery skipped after error: %s", type(exc).__name__)
            return {
                "skipped": False,
                "recovered_count": 0,
                "sessions": [],
                "error": f"Session recovery error: {type(exc).__name__}",
            }

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
        """Acquire a task lock atomically; an in-progress owner may re-enter its own lock."""
        locked_at = utcnow()
        result = await db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == task.status,
                or_(
                    Task.lock_owner.is_(None),
                    and_(Task.status == "in_progress", Task.lock_owner == worker_id),
                ),
            )
            .values(lock_owner=worker_id, locked_at=locked_at)
            .returning(Task.id)
            .execution_options(synchronize_session=False)
        )
        acquired = result.scalar_one_or_none() is not None
        if acquired:
            task.lock_owner = worker_id
            task.locked_at = locked_at
        return acquired

    async def release_lock(self, db: AsyncSession, task: Task) -> None:
        task.lock_owner = None
        task.locked_at = None
        await db.commit()

    async def run_orchestration_cycle(
        self,
        db: AsyncSession,
        *,
        force_session_recovery: bool = False,
    ) -> dict[str, Any]:
        """Assign ready queued tasks to idle enabled workers respecting dependencies, mode, skills & concurrency."""
        session_recovery = await self.recover_sessions_if_needed(db, force=force_session_recovery)

        settings = get_settings()
        agents_res = await db.execute(
            select(Agent).where(
                Agent.role == "worker",
                Agent.is_enabled.is_(True),
                Agent.is_emergency_stopped.is_(False),
                or_(Agent.device_id.is_(None), Agent.device_id == LOCAL_DEVICE_ID),
            )
        )
        workers = list(agents_res.scalars().all())
        idle_workers = [w for w in workers if w.status == "idle"]
        working_count = sum(1 for w in workers if w.status == "working")

        tasks_res = await db.execute(select(Task).where(Task.status.in_(["queued", "assigned", "in_progress"])))
        tasks = list(tasks_res.scalars().all())
        queued = [t for t in tasks if t.status == "queued"]
        queued.sort(key=lambda t: (-PRIORITY_WEIGHT.get(t.priority, 2), to_naive_utc(t.created_at) or to_naive_utc(utcnow())))

        assigned: list[dict[str, str]] = []
        available_slots = max(0, settings.MAX_CONCURRENT_WORKERS - working_count)

        for task in queued:
            if available_slots <= 0 or not idle_workers:
                break
            # Tasks pinned to a remote execution device are claimed by that device's
            # worker daemon; the local scheduler must never steal them.
            if task.device_id and task.device_id != LOCAL_DEVICE_ID:
                continue
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
                    # Use assignment context and observed failures; performance_score is an
                    # unmeasured default and must not be presented as actual telemetry.
                    def _score(w: Agent) -> tuple[int, int]:
                        div_match = 1 if task.division_code in (w.divisions or []) else 0
                        return (div_match, -w.tasks_failed)

                    chosen = max(idle_workers, key=_score)

            if not chosen or not await self.acquire_lock(db, task, chosen.id):
                continue

            dispatch_time = utcnow()
            worker_claim = await db.execute(
                update(Agent)
                .where(
                    Agent.id == chosen.id,
                    Agent.status == "idle",
                    Agent.current_task_id.is_(None),
                    Agent.is_enabled.is_(True),
                    Agent.is_emergency_stopped.is_(False),
                )
                .values(status="working", current_task_id=task.id, last_heartbeat=dispatch_time)
                .returning(Agent.id)
                .execution_options(synchronize_session=False)
            )
            if worker_claim.scalar_one_or_none() is None:
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id, Task.status == "queued", Task.lock_owner == chosen.id)
                    .values(lock_owner=None, locked_at=None)
                    .execution_options(synchronize_session=False)
                )
                task.lock_owner = None
                task.locked_at = None
                if chosen in idle_workers:
                    idle_workers.remove(chosen)
                continue

            chosen.status = "working"
            chosen.current_task_id = task.id
            chosen.last_heartbeat = dispatch_time
            ws = ensure_employee_workspace(chosen.id)
            task.assigned_agent_id = chosen.id
            task.status = "in_progress"
            task.started_at = task.started_at or dispatch_time
            task.checkpoints = list(task.checkpoints or []) + [
                {
                    "ts": dispatch_time.isoformat(),
                    "worker": chosen.id,
                    "mode": task.execution_mode,
                    "note": "Dispatched by TaskScheduler",
                }
            ]

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
        return {
            "dispatched": assigned,
            "working_workers": working_count + len(assigned),
            "idle_workers": len(idle_workers),
            "session_recovery": session_recovery,
        }

    async def dispatch_ready_tasks(
        self,
        db: AsyncSession,
        *,
        force_session_recovery: bool = False,
    ) -> dict[str, Any]:
        """Alias for `run_orchestration_cycle` that also runs the periodic degraded-session recovery hook."""
        return await self.run_orchestration_cycle(db, force_session_recovery=force_session_recovery)

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
        cutoff = to_naive_utc(utcnow() - timedelta(minutes=settings.STALLED_TASK_MINUTES))
        res = await db.execute(select(Task).where(Task.status == "in_progress"))
        recovered: list[str] = []
        for task in res.scalars().all():
            # Compare in Python: SQLite stores naive UTC strings, so a SQL-side comparison
            # against an aware cutoff is not reliable across backends.
            updated = to_naive_utc(task.updated_at)
            if updated is None or updated >= cutoff:
                continue
            await self.fail_and_retry_or_escalate(db, task, "Stalled task timeout detected; auto-requeued by self-healing engine.")
            recovered.append(task.id)
        return recovered

    @staticmethod
    async def _manager_context(db: AsyncSession, project_id: str | None) -> tuple[list[Agent], dict[str, Any]]:
        employee_result = await db.execute(select(Agent).where(Agent.role == "worker").order_by(Agent.id.asc()))
        employees = list(employee_result.scalars().all())
        task_stmt = select(Task).where(~Task.status.in_(["completed", "failed", "cancelled"]))
        if project_id:
            task_stmt = task_stmt.where(Task.project_id == project_id)
        task_result = await db.execute(task_stmt.order_by(Task.created_at.asc(), Task.id.asc()))
        open_tasks = list(task_result.scalars().all())

        return employees, {
            "project_id": project_id,
            "employees": [
                {
                    "id": employee.id,
                    "name": employee.name,
                    "specialty": employee.specialty,
                    "skills": list(employee.skills or []),
                    "divisions": list(employee.divisions or []),
                    "status": employee.status,
                    "is_enabled": employee.is_enabled,
                    "is_emergency_stopped": employee.is_emergency_stopped,
                }
                for employee in employees
            ],
            "open_tasks": [
                {
                    "id": task.id,
                    "project_id": task.project_id,
                    "title": task.title,
                    "description": task.description,
                    "division_code": task.division_code,
                    "status": task.status,
                    "assigned_agent_id": task.assigned_agent_id,
                    "dependencies": list(task.dependencies or []),
                    "expected_deliverables": list(task.expected_deliverables or []),
                }
                for task in open_tasks
            ],
        }

    @staticmethod
    def _manager_system_prompt(gated_categories: set[str]) -> str:
        instructions = [
            "You are manager-0, the AATM engineering-team manager.",
            "Understand the user's directive in any language, including Arabic, and write the reply in that same language.",
            "Return only one JSON object matching this exact shape, with no Markdown or extra keys:",
            '{"reply":"text for the user in the user\'s language","tasks":[{"title":"...","description":"...","division_code":"D02","assigned_agent_id":"worker-1","expected_deliverables":["..."],"depends_on":[0]}]}',
            "The tasks array may be empty when no new task is needed or the user needs clarification.",
            "Only assign a task to an enabled, non-emergency-stopped employee listed in the context; match their skills and divisions.",
            "depends_on contains zero-based indexes into this response's tasks array. Use an empty list when a task has no prerequisites.",
            "Do not duplicate an open task already listed in the context.",
            "Do not claim that work has been executed or completed; describe the plan or ask a clarifying question.",
        ]
        if gated_categories:
            instructions.append(
                "A high-risk action is awaiting approval (categories: "
                + ", ".join(sorted(gated_categories))
                + "). Do not propose or claim execution of tasks that perform that action."
            )
        return "\n".join(instructions)

    @staticmethod
    def _manager_user_prompt(content: str, context: dict[str, Any]) -> str:
        return (
            "User directive (preserve its language):\n"
            + content
            + "\n\nCurrent employees, skills, divisions, and open tasks (JSON):\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _json_response_text(content: str) -> str:
        cleaned = content.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
        return fenced.group(1).strip() if fenced else cleaned

    @staticmethod
    def _validate_manager_assignments(plan: ManagerPlan, employees: list[Agent]) -> None:
        employee_by_id = {employee.id: employee for employee in employees}
        for index, task in enumerate(plan.tasks):
            employee = employee_by_id.get(task.assigned_agent_id)
            if employee is None:
                raise ValueError(f"Task {index} uses an employee ID that is not in the provided team context.")
            if not employee.is_enabled:
                raise ValueError(f"Task {index} is assigned to disabled employee '{employee.id}'.")
            if employee.is_emergency_stopped:
                raise ValueError(f"Task {index} is assigned to emergency-stopped employee '{employee.id}'.")
            if employee.divisions and task.division_code not in employee.divisions:
                raise ValueError(
                    f"Task {index} uses division {task.division_code}, which is not listed for '{employee.id}'."
                )

    async def _has_configured_model_provider(self, db: AsyncSession) -> bool:
        status = await get_model_configuration_status(db)
        return bool(status["configured"])

    async def _request_manager_plan(
        self,
        db: AsyncSession,
        manager: Agent,
        employees: list[Agent],
        base_prompt: str,
        system_prompt: str,
    ) -> tuple[ManagerPlan | None, str]:
        correction_detail: str | None = None
        invalid_response = ""
        for attempt in range(2):
            prompt = base_prompt
            if correction_detail is not None:
                prompt += (
                    "\n\nYour previous response failed validation. Correct it now. "
                    "Return only a valid JSON object matching the required schema.\n"
                    f"Validation error: {correction_detail[:2_000]}\n"
                    f"Previous response: {invalid_response[:8_000]}"
                )
            request = ProviderRequest(
                prompt=prompt,
                system_prompt=system_prompt,
                model=manager.model_name,
                max_tokens=4_096,
                temperature=0.1,
                agent_id=manager.id,
                allow_diagnostic_fallback=False,
            )
            try:
                response: ProviderResponse = await self.provider_router.route_and_execute(
                    db,
                    request,
                    preferred_provider_id=manager.provider_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Manager directive provider request failed")
                return None, "provider_unavailable"

            if not response.success:
                return None, "provider_unavailable"

            try:
                plan = ManagerPlan.model_validate_json(self._json_response_text(response.content))
                self._validate_manager_assignments(plan, employees)
                return plan, ""
            except (ValidationError, ValueError) as exc:
                if attempt == 1:
                    logger.warning("Manager plan remained invalid after its correction round: %s", exc)
                    return None, "invalid_plan"
                correction_detail = str(exc)
                invalid_response = response.content

        return None, "invalid_plan"

    async def _create_manager_tasks(
        self,
        db: AsyncSession,
        project_id: str | None,
        plan: ManagerPlan,
    ) -> list[Task]:
        if not plan.tasks:
            return []
        if not project_id:
            raise ValueError("Select a project before creating tasks from this directive.")
        if await db.get(Project, project_id) is None:
            raise ValueError("The selected project does not exist; no tasks were created.")

        new_tasks = [
            Task(
                project_id=project_id,
                division_code=task_plan.division_code,
                title=task_plan.title,
                description=task_plan.description,
                priority="high",
                execution_mode="manual",
                status="queued",
                assigned_agent_id=task_plan.assigned_agent_id,
                dependencies=[],
                expected_deliverables=task_plan.expected_deliverables,
            )
            for task_plan in plan.tasks
        ]
        db.add_all(new_tasks)
        await db.flush()

        for task, task_plan in zip(new_tasks, plan.tasks, strict=True):
            task.dependencies = [new_tasks[index].id for index in task_plan.depends_on]
        await db.flush()

        for task in new_tasks:
            await self.validate_dependencies(db, task.id, task.dependencies)

        for task in new_tasks:
            db.add(
                AuditLog(
                    actor="manager-0",
                    action="task.create",
                    resource_type="task",
                    resource_id=task.id,
                    details={"title": task.title, "division_code": task.division_code, "source": "manager_chat"},
                )
            )
        return new_tasks

    async def _save_manager_reply(
        self,
        db: AsyncSession,
        project_id: str | None,
        content: str,
        actions: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> ChatMessage:
        metadata_json: dict[str, Any] = {"actions": actions}
        if metadata:
            metadata_json.update(metadata)
        message = ChatMessage(
            project_id=project_id,
            sender_type="manager",
            sender_id="manager-0",
            recipient_id="user",
            content=content,
            metadata_json=metadata_json,
        )
        db.add(message)
        await db.commit()
        await db.refresh(message)
        await event_bus.emit("chat.message", {"id": message.id, "sender": "manager-0", "content": message.content})
        return message

    async def handle_manager_directive(self, db: AsyncSession, project_id: str | None, content: str) -> ChatMessage:
        """Use manager-0 to interpret a directive into a validated task DAG and localized reply."""
        actions_taken: list[str] = []

        # Keep the regex gate as an independent safety layer, before model planning.
        gated_categories: set[str] = set()
        for pattern, action_type, title in HIGH_RISK_DIRECTIVE_RULES:
            if action_type in gated_categories or not pattern.search(content):
                continue
            gated_categories.add(action_type)
            approval = ApprovalRequest(
                project_id=project_id,
                action_type=action_type,
                title=title,
                description=f"Requested via Manager Chat directive: {content}",
                requested_by="manager-0",
                payload={"directive": content[:500], "action_type": action_type},
                status="pending",
            )
            db.add(approval)
            actions_taken.append(f"Created Approval Request ({action_type}) awaiting user authorization.")

        if gated_categories:
            # Keep approval records if the model is missing, the plan is invalid, or a cycle is rejected.
            await db.commit()

        manager = await db.get(Agent, "manager-0")
        if manager is None:
            actions_taken.append("Manager profile is missing; no tasks were created.")
            return await self._save_manager_reply(
                db,
                project_id,
                "The manager profile is unavailable, so I could not process this directive. No tasks were created.",
                actions_taken,
            )

        if isinstance(self.provider_router, ProviderRouter):
            model_status = await get_model_configuration_status(db)
            if not model_status["configured"]:
                actions_taken.append("NO_MODEL_CONFIGURED: No real AI model provider is configured; directive not dispatched.")
                return await self._save_manager_reply(
                    db,
                    project_id,
                    (
                        "No model provider configured (NO_MODEL_CONFIGURED). "
                        "Open the Provider Setup Wizard to connect Claude API, an OpenAI-compatible/Ollama endpoint, "
                        "or Arena Local Bridge with an upstream before issuing directives, so no tasks were created."
                    ),
                    actions_taken,
                    metadata={"model_status": model_status},
                )

        employees, context = await self._manager_context(db, project_id)
        prompt = self._manager_user_prompt(content, context)
        plan, error = await self._request_manager_plan(
            db,
            manager,
            employees,
            prompt,
            self._manager_system_prompt(gated_categories),
        )

        if plan is None:
            if error == "provider_unavailable":
                if await self._has_configured_model_provider(db):
                    reply = (
                        "I could not get a usable response from the configured model providers. "
                        "No tasks were created."
                    )
                else:
                    reply = "No model provider configured. I could not process this directive, so no tasks were created."
                actions_taken.append("No task plan was created because no usable model response was available.")
            else:
                reply = "I could not validate a task plan after one correction attempt. No tasks were created."
                actions_taken.append("The model response failed task-plan validation after one correction attempt.")
            return await self._save_manager_reply(db, project_id, reply, actions_taken)

        metadata: dict[str, Any] = {}
        created_tasks: list[Task] = []
        if gated_categories and plan.tasks:
            # Never let an unapproved high-risk request become executable work.
            actions_taken.append("Task creation deferred while the high-risk approval is pending.")
        else:
            try:
                created_tasks = await self._create_manager_tasks(db, project_id, plan)
            except ValueError as exc:
                await db.rollback()
                actions_taken.append("The proposed task plan was rejected.")
                return await self._save_manager_reply(
                    db,
                    project_id,
                    f"The proposed task plan was rejected: {exc} No tasks were created.",
                    actions_taken,
                )

        if created_tasks:
            await db.commit()
            task_ids = [task.id for task in created_tasks]
            actions_taken.extend(
                f"Planned task '{task.title}' in {task.division_code} assigned to {task.assigned_agent_id}."
                for task in created_tasks
            )
            metadata["task_ids"] = task_ids
        try:
            cycle = await self.run_orchestration_cycle(db)
            actions_taken.append(f"Orchestration cycle dispatched {len(cycle['dispatched'])} task(s).")
            metadata["orchestration"] = cycle
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Manager-triggered orchestration cycle failed")
            metadata["orchestration_error"] = "The orchestration cycle failed; any created tasks remain persisted."
            actions_taken.append("The orchestration cycle failed; created tasks remain persisted.")

        return await self._save_manager_reply(db, project_id, plan.reply, actions_taken, metadata)


task_scheduler = TaskScheduler()
