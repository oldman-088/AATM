"""Safe, structured, cancellable execution loop for dispatched agent messages."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.session import AsyncSessionLocal
from app.git_workflows.manager import git_workflow_manager
from app.models import (
    Agent,
    AgentMessage,
    GitWorkflowRecord,
    Project,
    Task,
)
from app.models.project import project_base_branch
from app.orchestration.review import review_service
from app.orchestration.scheduler import task_scheduler
from app.providers.base import ProviderRequest, ProviderResponse
from app.providers.router import ProviderRouter, get_model_configuration_status
from app.runtime.agent_protocol import (
    ActionProtocolError,
    ActionRejected,
    parse_action_response,
)
from app.runtime.agent_protocol import (
    safe_excerpt as _safe_excerpt,
)
from app.runtime.executor import AgentRuntimeExecutor, WorkspaceSandboxError, agent_runtime
from app.runtime.loop.actions import ActionsMixin
from app.runtime.loop.approvals import ApprovalsMixin
from app.runtime.loop.claim import ClaimMixin
from app.runtime.loop.common import (  # noqa: F401 - re-exported
    _IGNORED_TREE_DIRS,
    _MAX_CONTEXT_FILE_CHARS,
    _MAX_CONTEXT_FILES,
    _MAX_TREE_FILES,
    _SENSITIVE_PAYLOAD_KEY,
    NeedsHumanError,
    _block_for_human_impl,
    _file_tree,
    _path_for_action,
    _read_text_prefix,
    _relevant_file_contents,
    _role_template,
    _safe_approval_payload,
    _safe_workspace,
    logger,
)
from app.runtime.loop.context import ContextMixin


class AgentLoop(ClaimMixin, ContextMixin, ActionsMixin, ApprovalsMixin):
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
        self._pending_wake = None
        self.wakes_received = 0


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
                await self._wait_for_wake_or_poll()
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
            from app.browser.arena_sessions import is_arena_agent_worker

            if await is_arena_agent_worker(db, agent):
                # Arena Agent mode: the employee's own Arena session does the work on GitHub; the
                # orchestrator's arena runner monitors it (no local model loop, no local workspace).
                from app.orchestration.arena_runner import arena_runner

                if not await task_scheduler.acquire_lock(db, task, agent.id):
                    await self._unclaim_in_session(db, message)
                    return
                agent.status = "working"
                agent.current_task_id = task.id
                agent.last_heartbeat = utcnow()
                await db.commit()
                await arena_runner.run_message(db, message, task, agent)
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

            work_root = await self._resolve_execution_workspace(db, task, agent)
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

                prompt = await self._build_prompt(db, message, task, agent, history, dependency_handoffs, work_root=work_root)
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
                        preferred_provider_id=await self._preferred_provider_id(db, agent),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise RuntimeError(f"Provider routing failed: {type(exc).__name__}: {exc}") from exc
                if not response.success:
                    if (response.metadata or {}).get("code") == "needs_human":
                        raise NeedsHumanError(
                            str(response.metadata.get("browser_profile_id") or agent.browser_profile_id or ""),
                            response.error_message or "Browser profile needs a human.",
                        )
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
                    result, completed = await self._execute_action(db, message, task, agent, action, work_root=work_root)
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
                    target_status = "completed"
                    outcome_extra: dict[str, Any] = {}
                    done_summary = getattr(action, "summary", None) or result
                    is_review = task.task_type == "review"
                    is_follow_up = task.task_type == "follow_up"
                    if not is_review and task.worktree_path and Path(task.worktree_path).is_dir():
                        commit_res = await git_workflow_manager.commit_task_worktree(
                            task.worktree_path,
                            agent_name=agent.name,
                            task_id=task.id,
                            task_title=task.title,
                        )
                        if commit_res.get("ok") and commit_res.get("commit_sha"):
                            task.commit_sha = commit_res["commit_sha"]
                        outcome_extra["git_commit"] = commit_res

                        project = await db.get(Project, task.project_id)
                        proj_cfg = (project.config or {}) if project and isinstance(project.config, dict) else {}
                        auto_pr = bool(proj_cfg.get("auto_pr", True))
                        repo_slug = None
                        if project and project.repo_url and "github.com" in project.repo_url:
                            m_slug = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", project.repo_url)
                            if m_slug:
                                repo_slug = m_slug.group(1)

                        if commit_res.get("committed") and auto_pr and task.branch_name and not is_follow_up:
                            pr_res = await git_workflow_manager.push_and_create_draft_pr(
                                task.worktree_path,
                                branch_name=task.branch_name,
                                base_branch=project_base_branch(project),
                                title=f"AATM({agent.name}): {task.title}",
                                body=(
                                    f"{done_summary}\n\n"
                                    f"AATM-Task: {task.id}\n"
                                    f"Agent: {agent.id} ({agent.name})\n"
                                    f"Commit: {task.commit_sha or 'n/a'}"
                                ),
                                repo_slug=repo_slug,
                            )
                            outcome_extra["draft_pr"] = pr_res
                            if pr_res.get("ok"):
                                task.pr_number = pr_res.get("pr_number")
                                task.pr_url = pr_res.get("pr_url")
                                task.pr_status = pr_res.get("pr_status") or "draft"
                                target_status = "pr_open"

                        wf = (
                            await db.execute(select(GitWorkflowRecord).where(GitWorkflowRecord.task_id == task.id))
                        ).scalar_one_or_none()
                        if wf:
                            wf.last_commit_sha = task.commit_sha
                            wf.pr_number = task.pr_number
                            wf.pr_url = task.pr_url
                            wf.pr_status = task.pr_status or ("committed" if commit_res.get("committed") else "clean")

                    message.execution_status = "completed"
                    message.response_payload = {
                        **(message.response_payload or {}),
                        "outcome": result,
                        "iterations": history,
                        **outcome_extra,
                    }
                    await task_scheduler.complete_task(
                        db,
                        task,
                        summary=done_summary,
                        target_status=target_status,
                    )
                    await self._store_outcome_memory(db, task, agent, target_status, done_summary)
                    # pr_open tasks are picked up by the manager's orchestration cycle, which creates the review task.
                    if is_follow_up:
                        await review_service.on_follow_up_completed(db, task)
                    return

            raise RuntimeError(f"Agent iteration limit reached ({self.max_iterations}) before verified completion.")


























def _make_loop() -> AgentLoop:
    return AgentLoop()
