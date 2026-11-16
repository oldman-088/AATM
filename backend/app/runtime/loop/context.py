"""Workspace resolution, dependency handoffs, prompt building and iteration persistence for AgentLoop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.core.security import sanitize_untrusted_content
from app.db.base import utcnow
from app.git_workflows.manager import git_workflow_manager
from app.memory.engine import memory_engine
from app.models import (
    Agent,
    AgentMessage,
    GitWorkflowRecord,
    MemoryEntry,
    Project,
    Task,
    WorkspaceArtifact,
)
from app.orchestration.replacement import copy_workspace_handoff
from app.orchestration.review import review_service
from app.runtime.agent_protocol import (
    ACTION_PROTOCOL as _ACTION_PROTOCOL,
)
from app.runtime.agent_protocol import (
    build_system_prompt,
)
from app.runtime.agent_protocol import (
    redact_secrets as _redact_secrets,
)
from app.runtime.agent_protocol import (
    safe_excerpt as _safe_excerpt,
)
from app.runtime.loop.common import (
    _file_tree,
    _relevant_file_contents,
    _role_template,
    _safe_workspace,
)


class ContextMixin:
    """Mixin for AgentLoop; relies on attributes set in AgentLoop.__init__."""

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

    async def _resolve_execution_workspace(
        self,
        db: AsyncSession,
        task: Task,
        agent: Agent,
    ) -> Path:
        """Resolve the isolated execution directory: per-task git worktree if enabled, else employee workspace."""
        if task.worktree_path:
            existing_wt = Path(task.worktree_path).resolve()
            if existing_wt.is_dir():
                return existing_wt

        project = await db.get(Project, task.project_id)
        if project is not None:
            settings = get_settings()
            repo_dir = Path(project.repo_path).resolve()
            is_git_repo = (repo_dir / ".git").exists()
            proj_cfg = project.config if isinstance(project.config, dict) else {}
            explicit_mode = proj_cfg.get("worktree_mode")
            is_default_repo = project.repo_path in {".", str(settings.REPO_ROOT)} or repo_dir == settings.REPO_ROOT.resolve()
            should_use_worktree = is_git_repo and (explicit_mode is True or (not is_default_repo and explicit_mode is not False))

            if should_use_worktree:
                wt_res = await git_workflow_manager.create_task_worktree(
                    task.id,
                    task.title,
                    repo_path=repo_dir,
                    base_branch=project.default_branch or "main",
                    db=db,
                    actor=agent.id,
                )
                if wt_res.get("ok") and wt_res.get("worktree_path"):
                    task.worktree_path = str(wt_res["worktree_path"])
                    task.branch_name = str(wt_res["branch_name"])
                    agent.git_branch = task.branch_name
                    existing_wf = (
                        await db.execute(select(GitWorkflowRecord).where(GitWorkflowRecord.task_id == task.id))
                    ).scalar_one_or_none()
                    if existing_wf is None:
                        db.add(
                            GitWorkflowRecord(
                                project_id=project.id,
                                task_id=task.id,
                                branch_name=task.branch_name,
                                worktree_path=task.worktree_path,
                                owner_agent_id=agent.id,
                                base_branch=str(wt_res.get("base_branch") or project.default_branch or "main"),
                                pr_status="pending",
                            )
                        )
                    await db.commit()
                    return Path(task.worktree_path).resolve()
                raise RuntimeError(wt_res.get("error") or "Failed to provision task git worktree.")

        return _safe_workspace(agent.id)

    async def _build_prompt(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
        history: list[dict[str, Any]],
        dependency_handoffs: list[dict[str, Any]],
        *,
        work_root: Path | None = None,
    ) -> str:
        project = await db.get(Project, task.project_id)
        root = work_root if work_root is not None else _safe_workspace(agent.id)
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
        if task.task_type == "review":
            data["pull_request_review"] = await review_service.review_context(db, task)
        return json.dumps(data, ensure_ascii=False, default=str)

    @staticmethod
    def _system_prompt(agent: Agent) -> str:
        return build_system_prompt(agent.name, agent.role_template or agent.role, _ACTION_PROTOCOL)

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
        # Live agent log (Phase 7): the same redacted excerpt that is persisted, never raw output.
        await event_bus.emit(
            "agent.iteration",
            {
                "agent_id": agent.id,
                "task_id": task.id,
                "iteration": iteration,
                "action": action,
                "result": safe_result[:500],
                "failed": bool(correction),
            },
        )
        await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status, "task_id": task.id})
        return entry

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
