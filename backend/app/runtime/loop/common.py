"""Shared constants and helpers for the agent loop."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.core.security import sanitize_untrusted_content
from app.db.base import utcnow
from app.employees.catalog import ROLE_TEMPLATES
from app.models import (
    Agent,
    AgentMessage,
    AuditLog,
    Task,
)
from app.runtime.agent_protocol import (
    ActionRejected,
    safe_workspace_path,
)
from app.runtime.agent_protocol import (
    redact_secrets as _redact_secrets,
)
from app.runtime.agent_protocol import (
    safe_excerpt as _safe_excerpt,
)
from app.runtime.executor import WorkspaceSandboxError
from app.runtime.sandbox import is_sensitive_workspace_path, resolve_contained_path

# Keep the historical logger name so log filters/alerts keep matching.
logger = logging.getLogger("app.runtime.agent_loop")
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
class NeedsHumanError(RuntimeError):
    """The employee's Arena browser profile hit a human check; nothing may continue automatically."""

    def __init__(self, profile_id: str, message: str) -> None:
        super().__init__(message)
        self.profile_id = profile_id
class RepoAccessError(RuntimeError):
    """E43: this employee's Arena account cannot see the project's repository. The window itself is fine."""

    def __init__(self, profile_id: str, repo: str, message: str) -> None:
        super().__init__(message)
        self.profile_id = profile_id
        self.repo = repo


# E43: a worker whose Arena account cannot see a project's repository gets none of its tasks for this long;
# after that AATM tries again (the owner may have granted access in the meantime).
REPO_ACCESS_RETRY_HOURS = 2


def repo_access_denied(project: Any, now: Any = None) -> set[str]:
    """The agent ids whose Arena account recently could not see this project's repository."""
    from datetime import datetime, timedelta

    denied = ((getattr(project, "config", None) or {}).get("repo_access_denied") or {}) if project is not None else {}
    cutoff = (now or utcnow()) - timedelta(hours=REPO_ACCESS_RETRY_HOURS)
    active: set[str] = set()
    for agent_id, entry in denied.items():
        try:
            at = datetime.fromisoformat(str((entry or {}).get("at")))
        except ValueError:
            continue
        if at.tzinfo is None and cutoff.tzinfo is not None:
            at = at.replace(tzinfo=cutoff.tzinfo)
        if at > cutoff:
            active.add(agent_id)
    return active


async def _requeue_for_repo_access_impl(db: AsyncSession, message: AgentMessage, task: Task, agent: Agent,
                                        exc: RepoAccessError) -> None:
    """E43: record that this account cannot see the repo, free the worker and give the task to another one."""
    from sqlalchemy import select

    from app.models import Notification, Project

    now = utcnow()
    project = await db.get(Project, task.project_id) if task.project_id else None
    first = True
    if project is not None:
        first = agent.id not in repo_access_denied(project, now)
        config = dict(project.config or {})
        denied = dict(config.get("repo_access_denied") or {})
        denied[agent.id] = {"at": now.isoformat(), "profile_id": exc.profile_id, "repo": exc.repo,
                            "detail": _safe_excerpt(str(exc), 200)}
        project.config = {**config, "repo_access_denied": denied}
    blocked_for = repo_access_denied(project, now) if project is not None else {agent.id}
    candidates = (await db.execute(select(Agent).where(
        Agent.role == "worker", Agent.is_enabled.is_(True), Agent.browser_profile_id.is_not(None)))).scalars().all()
    remaining = [w.id for w in candidates if w.id not in blocked_for]

    message.execution_status = "cancelled"
    message.claimed_by = None
    message.claimed_at = None
    message.response_payload = {**(message.response_payload or {}), "repo_access_denied": exc.repo,
                                "browser_profile_id": exc.profile_id}
    if agent.current_task_id == task.id:
        agent.current_task_id = None
    agent.status = "idle"
    task.lock_owner = None
    task.locked_at = None
    if task.execution_mode == "manual" or not remaining:
        task.status = "blocked"
        task.error_message = (f"No Arena account that may take this task can see {exc.repo}. Grant the Arena GitHub app "
                              f"access to {exc.repo}, then re-queue the task.")[:1_000]
    else:
        task.status = "queued"
        task.assigned_agent_id = None
    task.checkpoints = list(task.checkpoints or []) + [
        {"ts": now.isoformat(), "event": "repo_access_denied", "agent_id": agent.id, "browser_profile_id": exc.profile_id,
         "repo": exc.repo, "requeued": task.status == "queued"}
    ]
    if first:
        db.add(Notification(
            category="arena", severity="critical", title=f"Arena cannot see {exc.repo}"[:200],
            message=(f"{agent.name} (profile {exc.profile_id}) cannot select {exc.repo} in Arena. Grant the Arena GitHub "
                     f"app access to this repository for that account. Its tasks go to other workers meanwhile.")[:1_000],
            link="/browsers"))
    db.add(AuditLog(actor=agent.id, action="arena.repo_access_denied", resource_type="task", resource_id=task.id,
                    details={"repo": exc.repo, "profile_id": exc.profile_id, "requeued": task.status == "queued",
                             "remaining_workers": len(remaining)}, severity="warning"))
    await db.commit()
    await event_bus.emit("task.updated", {"task_id": task.id, "status": task.status})
    await event_bus.emit("agent.heartbeat", {"agent_id": agent.id, "status": agent.status})


async def _block_for_human_impl(db: AsyncSession, message: AgentMessage, task: Task, agent: Agent, exc: NeedsHumanError) -> None:
    from app.models import BrowserProfile, Notification

    reason = _safe_excerpt(str(exc), 200)
    message.execution_status = "paused"
    message.claimed_by = None
    message.claimed_at = None
    message.response_payload = {**(message.response_payload or {}), "needs_human": True, "browser_profile_id": exc.profile_id}
    task.status = "blocked"
    task.lock_owner = None
    task.locked_at = None
    task.checkpoints = list(task.checkpoints or []) + [
        {"ts": utcnow().isoformat(), "event": "needs_human", "browser_profile_id": exc.profile_id, "reason": reason}
    ]
    if agent.current_task_id == task.id:
        agent.current_task_id = None
    agent.status = "needs_human"
    profile = await db.get(BrowserProfile, exc.profile_id) if exc.profile_id else None
    if profile is not None:
        profile.connector_status = "needs_human"
        profile.needs_human_reason = reason
    db.add(
        Notification(
            category="arena",
            severity="critical",
            title=f"{agent.name}: browser profile needs you",
            message=f"Profile {exc.profile_id} stopped ({reason}). Resolve it in the browser, then press Resume in the Browser Wall.",
            link="/browsers",
        )
    )
    db.add(
        AuditLog(
            actor=agent.id,
            action="arena.needs_human",
            resource_type="browser_profile",
            resource_id=exc.profile_id or None,
            details={"task_id": task.id, "reason": reason},
            severity="warning",
        )
    )
    await db.commit()
    await event_bus.emit("arena.needs_human", {"agent_id": agent.id, "profile_id": exc.profile_id, "task_id": task.id, "reason": reason})
