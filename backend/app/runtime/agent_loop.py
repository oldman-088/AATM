"""Safe, structured, cancellable execution loop for dispatched agent messages."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.core.events import event_bus
from app.core.security import sanitize_untrusted_content
from app.db.base import utcnow
from app.db.session import AsyncSessionLocal
from app.employees.catalog import ROLE_TEMPLATES
from app.memory.engine import memory_engine
from app.models import (
    Agent,
    AgentMessage,
    ApprovalRequest,
    AuditLog,
    MemoryEntry,
    Project,
    ProviderUsageLog,
    SystemSetting,
    Task,
    WorkspaceArtifact,
)
from app.orchestration.replacement import copy_workspace_handoff
from app.orchestration.scheduler import task_scheduler
from app.providers.base import ProviderRequest, ProviderResponse
from app.providers.router import ProviderRouter, get_model_configuration_status
from app.runtime.agent_protocol import (
    ACTION_PROTOCOL as _ACTION_PROTOCOL,
)
from app.runtime.agent_protocol import (
    MAX_READ_CHARS as _MAX_READ_CHARS,
)
from app.runtime.agent_protocol import (
    Action,
    ActionProtocolError,
    ActionRejected,
    ApprovalAction,
    DoneAction,
    MessageAction,
    ReadAction,
    RunAction,
    WriteAction,
    build_system_prompt,
    can_run_permissions,
    can_write_permissions,
    contains_credentials,
    parse_action_response,
    safe_workspace_path,
    validate_run_command,
)
from app.runtime.agent_protocol import (
    redact_secrets as _redact_secrets,
)
from app.runtime.agent_protocol import (
    safe_excerpt as _safe_excerpt,
)
from app.runtime.executor import AgentRuntimeExecutor, WorkspaceSandboxError, agent_runtime
from app.runtime.sandbox import is_sensitive_workspace_path, resolve_contained_path

logger = logging.getLogger(__name__)

_MAX_CONTEXT_FILE_CHARS = 3_000
_MAX_TREE_FILES = 200
_MAX_CONTEXT_FILES = 5
_IGNORED_TREE_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}
_SENSITIVE_PAYLOAD_KEY = re.compile(r"(?i)(?:api[_-]?key|secret|token|password|credential|private[_-]?key)")


def _safe_approval_payload(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[REDACTED: nested payload limit]"
    if isinstance(value, dict):
        return {
            str(key)[:120]: (
                "[REDACTED]" if _SENSITIVE_PAYLOAD_KEY.search(str(key)) else _safe_approval_payload(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_approval_payload(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _safe_excerpt(value, 2_000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _role_template(agent: Agent) -> dict[str, Any]:
    return next((template for template in ROLE_TEMPLATES if template["id"] == agent.role_template), {})


def _safe_workspace(agent_id: str) -> Path:
    root = resolve_contained_path(get_settings().WORKSPACES_DIR, agent_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_text_prefix(path: Path, limit: int) -> str:
    with path.open("r", encoding="utf-8") as source:
        return source.read(limit + 1)


def _path_for_action(root: Path, relative_path: str, *, for_write: bool = False) -> Path:
    return safe_workspace_path(root, relative_path, for_write=for_write)


def _file_tree(root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in root.rglob("*"):
        if any(part in _IGNORED_TREE_DIRS for part in item.relative_to(root).parts):
            continue
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(root).as_posix()
        if is_sensitive_workspace_path(relative):
            continue
        try:
            safe = resolve_contained_path(root, relative)
            result.append({"path": relative, "size_bytes": safe.stat().st_size})
        except (OSError, WorkspaceSandboxError):
            continue
        if len(result) >= _MAX_TREE_FILES:
            break
    return result


def _relevant_file_contents(root: Path, tree: list[dict[str, Any]], query: str) -> list[dict[str, str]]:
    query_words = {word for word in re.findall(r"[a-zA-Z0-9_]+", query.lower()) if len(word) > 2}
    ranked: list[tuple[int, str]] = []
    for entry in tree:
        path = entry["path"]
        suffix = Path(path).suffix.lower()
        if suffix not in {".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".ts", ".tsx", ".js", ".jsx", ".css"}:
            continue
        score = len(query_words & set(re.findall(r"[a-zA-Z0-9_]+", path.lower())))
        ranked.append((score, path))
    ranked.sort(key=lambda row: (-row[0], row[1]))

    selected = [path for score, path in ranked if score > 0][:_MAX_CONTEXT_FILES]
    if not selected:
        selected = [path for _, path in ranked[:_MAX_CONTEXT_FILES]]
    contents: list[dict[str, str]] = []
    for relative in selected:
        try:
            target = _path_for_action(root, relative)
            raw_text = _read_text_prefix(target, _MAX_CONTEXT_FILE_CHARS)
            text = raw_text[:_MAX_CONTEXT_FILE_CHARS]
            if len(raw_text) > _MAX_CONTEXT_FILE_CHARS:
                text += "\n[truncated]"
        except (OSError, UnicodeDecodeError, ActionRejected, WorkspaceSandboxError):
            continue
        contents.append(
            {
                "path": relative,
                "content": sanitize_untrusted_content(_redact_secrets(text), source=f"workspace:{relative}")["quarantined_text"],
            }
        )
    return contents


class AgentLoop:
    """Poll, atomically claim, and execute assigned agent messages until stopped."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        provider_router: ProviderRouter | None = None,
        executor: AgentRuntimeExecutor | None = None,
        poll_seconds: float | None = None,
        max_iterations: int | None = None,
        max_parallel: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        settings = get_settings()
        self.session_factory = session_factory
        self.provider_router = provider_router or ProviderRouter()
        self.executor = executor or agent_runtime
        self.poll_seconds = max(0.05, float(poll_seconds if poll_seconds is not None else settings.AGENT_LOOP_POLL_SECONDS))
        self.max_iterations = max(1, int(max_iterations if max_iterations is not None else settings.AGENT_MAX_ITERATIONS))
        configured_parallel = settings.AGENT_MAX_PARALLEL or settings.ARENA_BRIDGE_MAX_CONCURRENCY
        self.max_parallel = max(1, int(max_parallel if max_parallel is not None else configured_parallel))
        self.worker_id = worker_id or f"agent-loop-{uuid.uuid4().hex[:12]}"
        self._active: set[asyncio.Task[None]] = set()
        self._stop_event = asyncio.Event()

    async def claim_one(self) -> str | None:
        """Claim one eligible running task message with one conditional UPDATE ... RETURNING."""
        message_candidate = aliased(AgentMessage)
        candidate_id = (
            select(message_candidate.id)
            .join(Task, Task.id == message_candidate.task_id)
            .join(Agent, Agent.id == message_candidate.recipient_id)
            .where(
                message_candidate.execution_status == "running",
                message_candidate.claimed_by.is_(None),
                Task.status == "in_progress",
                Task.assigned_agent_id == message_candidate.recipient_id,
                or_(Task.device_id.is_(None), Task.device_id == "device-local"),
                or_(Agent.device_id.is_(None), Agent.device_id == "device-local"),
                Agent.is_enabled.is_(True),
                Agent.is_emergency_stopped.is_(False),
            )
            .order_by(
                case(
                    (message_candidate.priority == "critical", 0),
                    (message_candidate.priority == "high", 1),
                    (message_candidate.priority == "medium", 2),
                    else_=3,
                ),
                message_candidate.created_at.asc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        async with self.session_factory() as db:
            statement = (
                update(AgentMessage)
                .where(
                    AgentMessage.id == candidate_id,
                    AgentMessage.execution_status == "running",
                    AgentMessage.claimed_by.is_(None),
                )
                .values(claimed_by=self.worker_id, claimed_at=utcnow())
                .returning(AgentMessage.id)
            )
            claimed_id = (await db.execute(statement)).scalar_one_or_none()
            if claimed_id is None:
                await db.rollback()
                return None
            await db.commit()
            return str(claimed_id)

    async def process_one(self) -> bool:
        """Process at most one message synchronously; useful for deterministic operations/tests."""
        await self.resume_approval_messages()
        message_id = await self.claim_one()
        if message_id is None:
            return False
        await self.process_claimed_message(message_id)
        return True

    async def run_forever(self) -> None:
        """Run until the host cancels this task; in-flight work is cancelled cleanly."""
        self._stop_event.clear()
        try:
            while not self._stop_event.is_set():
                try:
                    await self.resume_approval_messages()
                    while len(self._active) < self.max_parallel and not self._stop_event.is_set():
                        message_id = await self.claim_one()
                        if message_id is None:
                            break
                        work = asyncio.create_task(
                            self.process_claimed_message(message_id),
                            name=f"aatm-agent-message-{message_id}",
                        )
                        self._active.add(work)
                        work.add_done_callback(self._active.discard)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Agent loop polling cycle failed")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    continue
        finally:
            self._stop_event.set()
            active = list(self._active)
            for work in active:
                work.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            self._active.clear()

    async def stop(self) -> None:
        self._stop_event.set()

    async def _unclaim(self, message_id: str) -> None:
        async with self.session_factory() as db:
            message = await db.get(AgentMessage, message_id)
            if message and message.execution_status == "running" and message.claimed_by == self.worker_id:
                message.claimed_by = None
                message.claimed_at = None
                await db.commit()

    async def process_claimed_message(self, message_id: str) -> None:
        try:
            await self._execute_message(message_id)
        except asyncio.CancelledError:
            await self._unclaim(message_id)
            raise
        except Exception as exc:
            logger.exception("Agent execution failed for message %s", message_id)
            await self._fail_unhandled(message_id, exc)

    async def _execute_message(self, message_id: str) -> None:
        async with self.session_factory() as db:
            message = await db.get(AgentMessage, message_id)
            if not message or message.execution_status != "running" or message.claimed_by != self.worker_id:
                return
            if not message.task_id:
                message.execution_status = "failed"
                message.response_payload = {"error": "Task execution messages require a task_id."}
                await db.commit()
                return

            task = await db.get(Task, message.task_id)
            agent = await db.get(Agent, message.recipient_id)
            if not task or not agent:
                raise RuntimeError("Claimed agent message references a missing task or employee.")
            if task.assigned_agent_id != agent.id:
                await self._unclaim_in_session(db, message)
                return
            if task.status != "in_progress":
                if task.status in {"blocked", "cancelled"}:
                    await self._pause_for_stop(db, message, task, agent, "Task is blocked or cancelled.")
                else:
                    await self._unclaim_in_session(db, message)
                return
            if not await task_scheduler.dependencies_satisfied(db, task):
                await self._unclaim_in_session(db, message)
                return
            if not agent.is_enabled or agent.is_emergency_stopped:
                await self._pause_for_stop(db, message, task, agent, "Employee is disabled or emergency-stopped.")
                return
            if agent.status == "working" and agent.current_task_id not in {None, task.id}:
                await self._unclaim_in_session(db, message)
                return
            if isinstance(self.provider_router, ProviderRouter):
                model_status = await get_model_configuration_status(db)
                if not model_status["configured"]:
                    raise RuntimeError(
                        "NO_MODEL_CONFIGURED: No real AI model provider is configured; refusing diagnostic_only execution."
                    )

            if not await task_scheduler.acquire_lock(db, task, agent.id):
                await self._unclaim_in_session(db, message)
                return
            agent.status = "working"
            agent.current_task_id = task.id
            agent.last_heartbeat = utcnow()
            await db.commit()

            dependency_handoffs = await self._prepare_dependency_handoffs(db, task, agent)
            history = list((message.response_payload or {}).get("iterations", []))
            first_iteration = len(history) + 1

            for iteration in range(first_iteration, self.max_iterations + 1):
                if await self._is_emergency_stopped(db, message, task, agent):
                    await self._pause_for_stop(db, message, task, agent, "Emergency stop checked before model iteration.", iteration)
                    return
                spent, cost_limit = await self._task_cost_and_limit(db, task.id)
                if spent >= cost_limit:
                    raise RuntimeError(
                        f"Per-task cost budget reached (${spent:.4f} of ${cost_limit:.4f}); execution stopped before another model turn."
                    )

                prompt = await self._build_prompt(db, message, task, agent, history, dependency_handoffs)
                request = ProviderRequest(
                    prompt=prompt,
                    system_prompt=self._system_prompt(agent),
                    model=agent.model_name,
                    max_tokens=2_048,
                    temperature=0.1,
                    agent_id=agent.id,
                    task_id=task.id,
                    allow_diagnostic_fallback=False,
                )
                try:
                    response: ProviderResponse = await self.provider_router.route_and_execute(
                        db,
                        request,
                        preferred_provider_id=agent.provider_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise RuntimeError(f"Provider routing failed: {type(exc).__name__}: {exc}") from exc
                if not response.success:
                    raise RuntimeError(response.error_message or "Provider routing returned no successful response.")

                if await self._is_emergency_stopped(db, message, task, agent):
                    await self._persist_iteration(
                        db,
                        message,
                        task,
                        agent,
                        iteration,
                        "emergency_stop",
                        "Emergency stop arrived during the provider request; no returned action was executed.",
                    )
                    await self._pause_for_stop(db, message, task, agent, "Emergency stop arrived during model execution.")
                    return

                spent, cost_limit = await self._task_cost_and_limit(db, task.id)
                if spent >= cost_limit:
                    raise RuntimeError(
                        f"Per-task cost budget reached (${spent:.4f} of ${cost_limit:.4f}); returned action was not executed."
                    )

                try:
                    action = parse_action_response(response.content)
                except ActionProtocolError as exc:
                    correction = (
                        f"Invalid action response: {exc} Return exactly one valid JSON object matching one supported action; "
                        "do not guess or include commentary."
                    )
                    record = await self._persist_iteration(
                        db, message, task, agent, iteration, "invalid", correction, correction=correction
                    )
                    history.append(record)
                    continue

                try:
                    result, completed = await self._execute_action(db, message, task, agent, action)
                except ActionRejected as exc:
                    correction = f"Action rejected by safety policy: {exc} Choose a permitted workspace action or request approval."
                    record = await self._persist_iteration(
                        db,
                        message,
                        task,
                        agent,
                        iteration,
                        action.action,
                        correction,
                        correction=correction,
                    )
                    history.append(record)
                    continue
                except WorkspaceSandboxError as exc:
                    correction = f"Workspace path or command rejected: {exc} Use a safe relative workspace path and an approved test/lint tool."
                    record = await self._persist_iteration(
                        db,
                        message,
                        task,
                        agent,
                        iteration,
                        action.action,
                        correction,
                        correction=correction,
                    )
                    history.append(record)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._persist_iteration(
                        db,
                        message,
                        task,
                        agent,
                        iteration,
                        action.action,
                        f"Action execution failed: {type(exc).__name__}: {_safe_excerpt(str(exc), 1_000)}",
                    )
                    raise

                if action.action == "approval":
                    await self._pause_for_approval(db, message, task, agent, action, iteration, result)
                    return

                record = await self._persist_iteration(
                    db,
                    message,
                    task,
                    agent,
                    iteration,
                    action.action,
                    result,
                )
                history.append(record)
                if completed:
                    message.execution_status = "completed"
                    message.response_payload = {
                        **(message.response_payload or {}),
                        "outcome": result,
                        "iterations": history,
                    }
                    await task_scheduler.complete_task(db, task, summary=action.summary)
                    await self._store_outcome_memory(db, task, agent, "completed", action.summary)
                    return

            raise RuntimeError(f"Agent iteration limit reached ({self.max_iterations}) before verified completion.")

    async def _is_emergency_stopped(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
    ) -> bool:
        await db.refresh(agent)
        await db.refresh(task)
        await db.refresh(message)
        return bool(
            agent.is_emergency_stopped
            or not agent.is_enabled
            or task.status != "in_progress"
            or (task.error_message or "").startswith("Emergency stop")
        )

    async def _unclaim_in_session(self, db: AsyncSession, message: AgentMessage) -> None:
        message.claimed_by = None
        message.claimed_at = None
        await db.commit()

    async def _pause_for_stop(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
        reason: str,
        iteration: int | None = None,
    ) -> None:
        payload = dict(message.response_payload or {})
        payload.pop("approval_id", None)
        payload.pop("pending_approval_id", None)
        payload["paused_reason"] = reason
        message.response_payload = payload
        message.execution_status = "paused"
        message.claimed_by = None
        message.claimed_at = None
        if task.status == "in_progress":
            task.status = "blocked"
            task.lock_owner = None
            task.locked_at = None
            task.error_message = f"Emergency stop: {reason}"
            task.checkpoints = list(task.checkpoints or []) + [
                {"ts": utcnow().isoformat(), "event": "agent_loop_paused", "reason": reason}
            ]
        if agent.current_task_id == task.id:
            agent.current_task_id = None
            agent.status = "paused" if agent.is_emergency_stopped else "idle"
        if iteration is not None:
            task.checkpoints = list(task.checkpoints or []) + [
                {"ts": utcnow().isoformat(), "event": "agent_iteration_stopped", "iteration": iteration}
            ]
        agent.last_heartbeat = utcnow()
        await db.commit()
        await event_bus.emit("task.updated", {"task_id": task.id, "status": task.status, "paused": True})
        await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status})

    async def _task_cost_and_limit(self, db: AsyncSession, task_id: str) -> tuple[float, float]:
        spent = float(
            (
                await db.execute(
                    select(func.coalesce(func.sum(ProviderUsageLog.estimated_cost_usd), 0.0)).where(
                        ProviderUsageLog.task_id == task_id
                    )
                )
            ).scalar_one()
            or 0.0
        )
        default_limit = float(get_settings().AGENT_TASK_BUDGET_USD)
        budgets = await db.get(SystemSetting, "budgets")
        configured = budgets.value.get("per_task_limit_usd") if budgets and isinstance(budgets.value, dict) else None
        try:
            limit = float(configured) if configured is not None else default_limit
        except (TypeError, ValueError):
            limit = default_limit
        if not math.isfinite(limit) or limit < 0:
            limit = default_limit
        return spent, limit

    async def _prepare_dependency_handoffs(
        self,
        db: AsyncSession,
        task: Task,
        agent: Agent,
    ) -> list[dict[str, Any]]:
        if not task.dependencies:
            return []
        result = await db.execute(select(Task).where(Task.id.in_(task.dependencies)))
        dependencies = list(result.scalars().all())
        handoffs: list[dict[str, Any]] = []
        for dependency in dependencies:
            path = None
            files: list[str] = []
            if dependency.assigned_agent_id and dependency.assigned_agent_id != agent.id:
                path, files = copy_workspace_handoff(dependency.assigned_agent_id, agent.id)
            handoffs.append(
                {
                    "task_id": dependency.id,
                    "title": dependency.title,
                    "status": dependency.status,
                    "summary": sanitize_untrusted_content(
                        _redact_secrets(dependency.result_summary or ""), source=f"task:{dependency.id}:result"
                    )["quarantined_text"],
                    "deliverables": list(dependency.actual_deliverables or []),
                    "handoff_path": path,
                    "handoff_files": files,
                }
            )
        return handoffs

    async def _build_prompt(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
        history: list[dict[str, Any]],
        dependency_handoffs: list[dict[str, Any]],
    ) -> str:
        project = await db.get(Project, task.project_id)
        root = _safe_workspace(agent.id)
        tree = _file_tree(root)
        query = f"{task.title} {task.description} {message.instructions}"
        file_contents = _relevant_file_contents(root, tree, query)
        memories = await memory_engine.search(
            db,
            query=query,
            project_id=task.project_id,
            requesting_agent_id=agent.id,
            limit=5,
        )
        template = _role_template(agent)
        data = {
            "employee": {
                "id": agent.id,
                "name": agent.name,
                "role": agent.role,
                "role_template": agent.role_template,
                "role_template_title": template.get("title", agent.specialty),
                "template_skills": template.get("skills", []),
                "skills": agent.skills or [],
                "template_permissions": template.get("permissions", []),
                "permissions": agent.permissions or [],
                "message_permissions": message.permissions or [],
                "autonomy_mode": agent.autonomy_mode,
            },
            "project": {
                "id": project.id if project else task.project_id,
                "name": project.name if project else "",
                "description": _redact_secrets(project.description) if project else "",
                "tech_stack": project.tech_stack if project else [],
            },
            "task": {
                "id": task.id,
                "title": sanitize_untrusted_content(task.title, source="task:title")["quarantined_text"],
                "description": sanitize_untrusted_content(task.description, source="task:description")["quarantined_text"],
                "instructions": sanitize_untrusted_content(message.instructions, source="agent_message")["quarantined_text"],
                "priority": task.priority,
                "execution_mode": task.execution_mode,
                "expected_deliverables": task.expected_deliverables or [],
                "dependencies": task.dependencies or [],
            },
            "dependency_results": dependency_handoffs,
            "workspace_file_tree": tree,
            "relevant_workspace_file_contents": file_contents,
            "relevant_memories_top_k": [
                {
                    "title": _redact_secrets(str(memory["title"])),
                    "content": sanitize_untrusted_content(
                        _redact_secrets(str(memory["content"])), source=f"memory:{memory['id']}"
                    )["quarantined_text"],
                    "scope": memory["scope"],
                    "importance": memory["importance"],
                }
                for memory in memories
            ],
            "handoff_files": (message.workspace_info or {}).get("handoff_files", []),
            "approval_decision": (message.response_payload or {}).get("approval_decision"),
            "previous_iterations": history[-8:],
            "action_protocol": _ACTION_PROTOCOL,
        }
        return json.dumps(data, ensure_ascii=False, default=str)

    @staticmethod
    def _system_prompt(agent: Agent) -> str:
        return build_system_prompt(agent.name, agent.role_template or agent.role, _ACTION_PROTOCOL)

    async def _execute_action(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
        action: Action,
    ) -> tuple[str, bool]:
        root = _safe_workspace(agent.id)
        if isinstance(action, ReadAction):
            target = _path_for_action(root, action.path)
            if not target.is_file():
                raise ActionRejected(f"File '{action.path}' does not exist in the employee workspace.")
            try:
                raw_content = _read_text_prefix(target, _MAX_READ_CHARS)
            except UnicodeDecodeError as exc:
                raise ActionRejected("Only UTF-8 text files may be read through the action protocol.") from exc
            content = _safe_excerpt(raw_content[:_MAX_READ_CHARS], _MAX_READ_CHARS)
            if len(raw_content) > _MAX_READ_CHARS:
                content += "\n[truncated]"
            db.add(
                WorkspaceArtifact(
                    agent_id=agent.id,
                    project_id=task.project_id,
                    task_id=task.id,
                    relative_path=action.path,
                    action="read",
                    summary=f"Read {min(len(content), _MAX_READ_CHARS)} characters from a workspace text file.",
                    exit_code=0,
                )
            )
            return f"Read workspace file {action.path}:\n{content}", False

        if isinstance(action, WriteAction):
            if not self._can_write(agent):
                raise ActionRejected("This employee's role permissions do not allow workspace file edits.")
            target = _path_for_action(root, action.path, for_write=True)
            if contains_credentials(action.content):
                raise ActionRejected("Content that appears to contain credentials or private keys cannot be written.")
            existed = target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.content, encoding="utf-8")
            db.add(
                WorkspaceArtifact(
                    agent_id=agent.id,
                    project_id=task.project_id,
                    task_id=task.id,
                    relative_path=action.path,
                    action="modify" if existed else "create",
                    summary=f"Wrote {len(action.content)} characters to {action.path}.",
                    exit_code=0,
                )
            )
            return f"Wrote {len(action.content)} characters to {action.path}.", False

        if isinstance(action, RunAction):
            if not self._can_run(agent):
                raise ActionRejected("This employee's role permissions do not allow test or lint execution.")
            safe_command = self._validate_run_command(root, action.command)
            result = await self.executor.run_workspace_command(
                db,
                agent.id,
                safe_command,
                project_id=task.project_id,
                task_id=task.id,
            )
            text = (
                f"Command exit code: {result['exit_code']}\n"
                f"stdout:\n{_safe_excerpt(result.get('stdout'))}\n"
                f"stderr:\n{_safe_excerpt(result.get('stderr'))}"
            )
            if int(result["exit_code"]) != 0:
                text += "\nThe command failed. Inspect the recorded output, fix the cause, and run the test/lint command again."
            return text, False

        if isinstance(action, MessageAction):
            collaborators = set(task.collaborator_ids or [])
            if action.recipient_id not in collaborators:
                raise ActionRejected("Messages may only be sent to collaborators explicitly assigned to this task.")
            recipient = await db.get(Agent, action.recipient_id)
            if recipient is None or not recipient.is_enabled or recipient.is_emergency_stopped:
                raise ActionRejected("The requested collaborator is unavailable or stopped.")
            handoff_path, copied_files = copy_workspace_handoff(agent.id, recipient.id)
            delegated = AgentMessage(
                task_id=task.id,
                project_id=task.project_id,
                sender_id=agent.id,
                recipient_id=recipient.id,
                channel_type="group",
                instructions=action.instructions,
                priority=task.priority,
                required_tools=["workspace_fs", "python_runner"],
                workspace_info={"workspace_path": str(_safe_workspace(recipient.id)), "handoff_path": handoff_path, "handoff_files": copied_files},
                expected_deliverables=task.expected_deliverables or [],
                permissions=recipient.permissions or [],
                execution_status="delivered",
                delivery_status="delivered",
                response_payload={"handoff_path": handoff_path, "handoff_files": copied_files},
                delivered_at=utcnow(),
            )
            db.add(delegated)
            await db.flush()
            return f"Message {delegated.id} delivered to collaborator {recipient.id}; safe files are available under {handoff_path}/.", False

        if isinstance(action, ApprovalAction):
            approval = ApprovalRequest(
                project_id=task.project_id,
                action_type=action.action_type,
                title=_safe_excerpt(action.title, 200),
                description=_safe_excerpt(action.description, 4_000),
                requested_by=agent.id,
                estimated_cost_usd=action.estimated_cost_usd,
                payload={
                    **_safe_approval_payload(action.payload),
                    "agent_id": agent.id,
                    "task_id": task.id,
                    "requested_action_type": action.action_type,
                },
                status="pending",
            )
            db.add(approval)
            await db.flush()
            message.response_payload = {
                **(message.response_payload or {}),
                "pending_approval_id": approval.id,
            }
            return f"Approval request {approval.id} is pending; the protected action was not executed.", False

        if isinstance(action, DoneAction):
            declared = list(dict.fromkeys([*(task.expected_deliverables or []), *action.deliverables]))
            if not declared:
                artifacts = (
                    await db.execute(
                        select(WorkspaceArtifact.relative_path).where(
                            WorkspaceArtifact.task_id == task.id,
                            WorkspaceArtifact.action.in_(["create", "modify"]),
                        )
                    )
                ).scalars().all()
                declared = list(dict.fromkeys(artifacts))
            missing: list[str] = []
            verified: list[str] = []
            for relative in declared:
                try:
                    path = _path_for_action(root, relative)
                except (ActionRejected, WorkspaceSandboxError):
                    missing.append(relative)
                    continue
                if not path.is_file():
                    missing.append(relative)
                else:
                    verified.append(relative)
            if missing:
                return (
                    "Completion not verified. Missing or unsafe declared deliverables: "
                    + ", ".join(missing[:20])
                    + ". Create the files inside the workspace, then return done again.",
                    False,
                )

            test_files = self._existing_test_files(root)
            if test_files:
                test_result = await self.executor.run_workspace_command(
                    db,
                    agent.id,
                    ["pytest", "-q", *test_files],
                    project_id=task.project_id,
                    task_id=task.id,
                )
                if int(test_result["exit_code"]) != 0:
                    raise RuntimeError(
                        "Final workspace test verification failed: "
                        + _safe_excerpt(test_result.get("stdout"))
                        + " "
                        + _safe_excerpt(test_result.get("stderr"))
                    )
            task.actual_deliverables = verified
            summary = action.summary
            if test_files:
                summary = f"{summary} Verified {len(verified)} deliverable(s) and passed {len(test_files)} workspace test file(s)."
            else:
                summary = f"{summary} Verified {len(verified)} deliverable(s); no workspace test files were present."
            return summary, True

        raise ActionRejected("Action type is not supported.")

    @staticmethod
    def _can_write(agent: Agent) -> bool:
        return can_write_permissions(agent.permissions)

    @staticmethod
    def _can_run(agent: Agent) -> bool:
        return can_run_permissions(agent.permissions)

    @staticmethod
    def _validate_run_command(root: Path, command: list[str]) -> list[str]:
        return validate_run_command(root, command)

    @staticmethod
    def _existing_test_files(root: Path) -> list[str]:
        files: list[str] = []
        for item in sorted(root.rglob("*.py")):
            relative = item.relative_to(root).as_posix()
            if item.is_symlink() or is_sensitive_workspace_path(relative):
                continue
            if not (item.name.startswith("test_") or item.name.endswith("_test.py")):
                continue
            try:
                safe = resolve_contained_path(root, relative)
            except WorkspaceSandboxError:
                continue
            if safe.is_file():
                files.append(relative)
        return files

    async def _persist_iteration(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
        iteration: int,
        action: str,
        result: str,
        correction: str | None = None,
    ) -> dict[str, Any]:
        safe_result = _safe_excerpt(result, 4_000)
        entry: dict[str, Any] = {"iteration": iteration, "action": action, "result": safe_result}
        if correction:
            entry["correction"] = _safe_excerpt(correction, 1_000)
        payload = dict(message.response_payload or {})
        history = list(payload.get("iterations", []))
        history.append(entry)
        payload["iterations"] = history[-self.max_iterations :]
        if correction:
            payload["last_correction"] = _safe_excerpt(correction, 1_000)
        else:
            payload.pop("last_correction", None)
        message.response_payload = payload
        task.progress_percent = min(95, max(task.progress_percent, int(iteration / self.max_iterations * 95)))
        task.checkpoints = list(task.checkpoints or []) + [
            {
                "ts": utcnow().isoformat(),
                "event": "agent_iteration",
                "agent_id": agent.id,
                "iteration": iteration,
                "action": action,
                "result": safe_result[:500],
            }
        ]
        agent.last_heartbeat = utcnow()
        db.add(
            WorkspaceArtifact(
                agent_id=agent.id,
                project_id=task.project_id,
                task_id=task.id,
                relative_path=".",
                action="agent_iteration",
                summary=f"Iteration {iteration}: {action} — {safe_result[:700]}",
                exit_code=1 if correction else 0,
            )
        )
        await db.commit()
        await event_bus.emit(
            "task.updated",
            {"task_id": task.id, "status": task.status, "progress_percent": task.progress_percent, "iteration": iteration},
        )
        await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status, "task_id": task.id})
        return entry

    async def _pause_for_approval(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
        action: ApprovalAction,
        iteration: int,
        result: str,
    ) -> None:
        payload = dict(message.response_payload or {})
        approval_id = payload.pop("pending_approval_id", None)
        approval = await db.get(ApprovalRequest, str(approval_id)) if approval_id else None
        if approval is None:
            raise RuntimeError("Approval action did not persist an ApprovalRequest.")
        payload["approval_id"] = approval.id
        message.response_payload = payload
        message.execution_status = "paused"
        message.claimed_by = None
        message.claimed_at = None
        task.status = "blocked"
        task.lock_owner = None
        task.locked_at = None
        task.checkpoints = list(task.checkpoints or []) + [
            {
                "ts": utcnow().isoformat(),
                "event": "approval_requested",
                "approval_id": approval.id,
                "iteration": iteration,
                "action_type": action.action_type,
            }
        ]
        if agent.current_task_id == task.id:
            agent.current_task_id = None
            agent.status = "idle"
        agent.last_heartbeat = utcnow()
        await self._persist_iteration(
            db,
            message,
            task,
            agent,
            iteration,
            "approval",
            result,
        )
        db.add(
            AuditLog(
                actor=agent.id,
                action="agent.approval_requested",
                resource_type="approval_request",
                resource_id=approval.id,
                details={"task_id": task.id, "action_type": action.action_type},
            )
        )
        await db.commit()
        await event_bus.emit("task.updated", {"task_id": task.id, "status": "blocked", "approval_id": approval.id})
        await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status, "task_id": task.id})

    async def resume_approval_messages(self) -> int:
        """Resume approved messages after a decision; rejected requests cancel their task."""
        resumed = 0
        async with self.session_factory() as db:
            result = await db.execute(select(AgentMessage).where(AgentMessage.execution_status == "paused"))
            messages = list(result.scalars().all())
            for message in messages:
                payload = dict(message.response_payload or {})
                approval_id = payload.get("approval_id")
                if not approval_id:
                    continue
                approval = await db.get(ApprovalRequest, str(approval_id))
                if not approval or approval.status == "pending":
                    continue
                task = await db.get(Task, message.task_id) if message.task_id else None
                agent = await db.get(Agent, message.recipient_id)
                if task is None or agent is None:
                    continue
                if approval.status == "approved":
                    if agent.is_emergency_stopped or not agent.is_enabled:
                        continue
                    if agent.status == "working" and agent.current_task_id != task.id:
                        continue
                    if not await task_scheduler.acquire_lock(db, task, agent.id):
                        continue
                    task.status = "in_progress"
                    task.error_message = None
                    message.execution_status = "running"
                    message.claimed_by = None
                    message.claimed_at = None
                    payload["approval_decision"] = {"status": "approved", "review_note": approval.review_note}
                    message.response_payload = payload
                    agent.status = "working"
                    agent.current_task_id = task.id
                    agent.last_heartbeat = utcnow()
                    db.add(
                        AuditLog(
                            actor=approval.reviewed_by or "approval-center",
                            action="agent.approval_resumed",
                            resource_type="task",
                            resource_id=task.id,
                            details={"approval_id": approval.id, "agent_id": agent.id},
                        )
                    )
                    await db.commit()
                    await event_bus.emit("task.updated", {"task_id": task.id, "status": "in_progress", "approval_id": approval.id})
                    await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status, "task_id": task.id})
                    resumed += 1
                elif approval.status == "rejected":
                    task.status = "cancelled"
                    task.error_message = f"Approval rejected: {approval.review_note or approval.title}"
                    task.lock_owner = None
                    task.locked_at = None
                    message.execution_status = "rejected"
                    payload["approval_decision"] = {"status": "rejected", "review_note": approval.review_note}
                    message.response_payload = payload
                    message.claimed_by = None
                    message.claimed_at = None
                    if agent.current_task_id == task.id:
                        agent.current_task_id = None
                        agent.status = "idle"
                    agent.last_heartbeat = utcnow()
                    await db.commit()
                    await self._store_outcome_memory(db, task, agent, "cancelled", task.error_message)
                    await event_bus.emit("task.updated", {"task_id": task.id, "status": "cancelled", "approval_id": approval.id})
                    await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status})
        return resumed

    async def _store_outcome_memory(
        self,
        db: AsyncSession,
        task: Task,
        agent: Agent,
        status: str,
        summary: str,
    ) -> None:
        db.add(
            MemoryEntry(
                scope="task",
                visibility="project",
                project_id=task.project_id,
                agent_id=agent.id,
                task_id=task.id,
                title=f"Task outcome: {task.title[:170]}",
                content=_redact_secrets(f"Status: {status}. Outcome: {summary}")[:2_000],
                tags=["agent-outcome", status],
                importance=7 if status == "completed" else 5,
                created_by=agent.id,
            )
        )
        await db.commit()

    async def _fail_unhandled(self, message_id: str, exc: Exception) -> None:
        async with self.session_factory() as db:
            await db.rollback()
            message = await db.get(AgentMessage, message_id)
            if not message or message.execution_status not in {"running", "paused"}:
                return
            task = await db.get(Task, message.task_id) if message.task_id else None
            agent = await db.get(Agent, message.recipient_id)
            if task is None or agent is None:
                message.execution_status = "failed"
                message.response_payload = {"error": f"{type(exc).__name__}: {_safe_excerpt(str(exc), 1_000)}"}
                await db.commit()
                return
            if agent.is_emergency_stopped or task.status in {"blocked", "cancelled"}:
                await self._pause_for_stop(db, message, task, agent, "Emergency stop took precedence over execution failure.")
                return
            error = f"{type(exc).__name__}: {_safe_excerpt(str(exc), 1_000)}"
            message.execution_status = "failed"
            message.response_payload = {**(message.response_payload or {}), "error": error}
            await task_scheduler.fail_and_retry_or_escalate(db, task, error)
            await self._store_outcome_memory(db, task, agent, "failed", error)


def _make_loop() -> AgentLoop:
    return AgentLoop()
