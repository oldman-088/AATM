"""Execution of structured agent actions (read/write/run/done/review…) and completion verification helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import (
    Agent,
    AgentMessage,
    ApprovalRequest,
    Task,
    WorkspaceArtifact,
)
from app.orchestration.replacement import copy_workspace_handoff
from app.orchestration.review import review_service
from app.runtime.agent_protocol import (
    MAX_READ_CHARS as _MAX_READ_CHARS,
)
from app.runtime.agent_protocol import (
    Action,
    ActionRejected,
    ApprovalAction,
    DoneAction,
    MessageAction,
    ReadAction,
    ReviewAction,
    RunAction,
    WriteAction,
    can_run_permissions,
    can_write_permissions,
    contains_credentials,
    validate_run_command,
)
from app.runtime.agent_protocol import (
    safe_excerpt as _safe_excerpt,
)
from app.runtime.code_stream import (
    MAX_FRAME_BYTES,
    build_review_frame,
    build_write_frame,
    code_stream,
)
from app.runtime.executor import WorkspaceSandboxError
from app.runtime.loop.common import (
    _IGNORED_TREE_DIRS,
    _path_for_action,
    _read_text_prefix,
    _safe_approval_payload,
    _safe_workspace,
)
from app.runtime.sandbox import is_sensitive_workspace_path, resolve_contained_path


class ActionsMixin:
    """Mixin for AgentLoop; relies on attributes set in AgentLoop.__init__."""

    async def _execute_action(
        self,
        db: AsyncSession,
        message: AgentMessage,
        task: Task,
        agent: Agent,
        action: Action,
        *,
        work_root: Path | None = None,
    ) -> tuple[str, bool]:
        root = work_root if work_root is not None else _safe_workspace(agent.id)
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
            if task.task_type == "review":
                raise ActionRejected("Review tasks are read-only; describe required changes in a review action instead.")
            if not self._can_write(agent):
                raise ActionRejected("This employee's role permissions do not allow workspace file edits.")
            target = _path_for_action(root, action.path, for_write=True)
            if contains_credentials(action.content):
                raise ActionRejected("Content that appears to contain credentials or private keys cannot be written.")
            existed = target.exists()
            previous_content: str | None = None
            if existed:
                try:
                    # Bounded read: only the previous version is needed for a diff hunk.
                    if target.stat().st_size <= MAX_FRAME_BYTES * 4:
                        previous_content = target.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    previous_content = None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.content, encoding="utf-8")
            # B1: stream the real write/edit to the Command Bridge (redacted, capped, sensitive paths dropped).
            await code_stream.publish(
                build_write_frame(
                    agent_id=agent.id,
                    task_id=task.id,
                    path=action.path,
                    content=action.content,
                    previous_content=previous_content if existed else None,
                    action="edit" if existed else "write",
                )
            )
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
            exec_kwargs: dict[str, Any] = {
                "project_id": task.project_id,
                "task_id": task.id,
            }
            if task.worktree_path:
                exec_kwargs["workspace_root"] = root
            result = await self.executor.run_workspace_command(
                db,
                agent.id,
                safe_command,
                **exec_kwargs,
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

        if isinstance(action, ReviewAction):
            if task.task_type != "review":
                raise ActionRejected("The review action is only available on pull-request review tasks.")
            outcome = await review_service.submit_review(db, task, agent, event=action.event, body=action.body)
            await code_stream.publish(
                build_review_frame(agent_id=agent.id, task_id=task.id, body=action.body, event=action.event)
            )
            message.response_payload = {**(message.response_payload or {}), "review_outcome": outcome}
            next_step = (
                f"follow-up task {outcome['follow_up_task_id']} created for the author"
                if outcome.get("follow_up_task_id")
                else f"merge approval {outcome.get('merge_approval_id')} awaits a human decision"
            )
            return f"Review submitted ({action.event}); {next_step}.", True

        if isinstance(action, DoneAction):
            if task.task_type == "review":
                return (
                    "Review tasks finish with a review action (COMMENT or REQUEST_CHANGES), not done.",
                    False,
                )
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
                exec_kwargs: dict[str, Any] = {
                    "project_id": task.project_id,
                    "task_id": task.id,
                }
                if task.worktree_path:
                    exec_kwargs["workspace_root"] = root
                test_result = await self.executor.run_workspace_command(
                    db,
                    agent.id,
                    ["pytest", "-q", *test_files],
                    **exec_kwargs,
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
            if any(part in _IGNORED_TREE_DIRS for part in item.relative_to(root).parts):
                continue
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
