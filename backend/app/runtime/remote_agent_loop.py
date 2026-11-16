"""Device-local AgentLoop execution using the central server's model router.

The daemon owns only a configured workspace jail. It requests each model turn from the
central API, parses the shared structured action protocol, runs safe file/test actions
locally, and reports each iteration back through the device-authenticated API.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core.security import sanitize_untrusted_content
from app.runtime.agent_protocol import (
    MAX_READ_CHARS,
    ActionProtocolError,
    ActionRejected,
    ApprovalAction,
    DoneAction,
    MessageAction,
    ReadAction,
    RunAction,
    WriteAction,
    can_run_permissions,
    can_write_permissions,
    contains_credentials,
    parse_action_response,
    safe_excerpt,
    safe_workspace_path,
    validate_run_command,
)
from app.runtime.sandbox import WorkspaceSandboxError, is_sensitive_workspace_path, resolve_contained_path

_MAX_TREE_FILES = 200
_MAX_CONTEXT_FILES = 5
_MAX_CONTEXT_FILE_CHARS = 3_000
_IGNORED_TREE_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}
_TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".css",
}
_SECRET_WORDS = re.compile(r"(?i)api[_-]?key|secret|token|password|credential|private[_-]?key")

ModelCall = Callable[[dict[str, Any]], Awaitable[str]]
ProgressReporter = Callable[[dict[str, Any]], Awaitable[None]]
ToolRunner = Callable[[list[str]], Awaitable[dict[str, Any]]]


def _workspace_tree(root: Path) -> list[dict[str, Any]]:
    tree: list[dict[str, Any]] = []
    for item in root.rglob("*"):
        relative_parts = item.relative_to(root).parts
        if any(part in _IGNORED_TREE_DIRS for part in relative_parts):
            continue
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(root).as_posix()
        if is_sensitive_workspace_path(relative):
            continue
        try:
            safe = resolve_contained_path(root, relative)
            tree.append({"path": relative, "size_bytes": safe.stat().st_size})
        except (OSError, WorkspaceSandboxError):
            continue
        if len(tree) >= _MAX_TREE_FILES:
            break
    return tree


def _workspace_context(root: Path, task: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    tree = _workspace_tree(root)
    query = f"{task.get('title', '')} {task.get('description', '')}"
    query_words = {word for word in re.findall(r"[a-zA-Z0-9_]+", query.lower()) if len(word) > 2}
    ranked: list[tuple[int, str]] = []
    for entry in tree:
        path = str(entry["path"])
        if Path(path).suffix.lower() not in _TEXT_SUFFIXES:
            continue
        path_words = set(re.findall(r"[a-zA-Z0-9_]+", path.lower()))
        ranked.append((len(query_words & path_words), path))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = [path for score, path in ranked if score > 0][:_MAX_CONTEXT_FILES]
    if not selected:
        selected = [path for _, path in ranked[:_MAX_CONTEXT_FILES]]

    contents: list[dict[str, str]] = []
    for relative in selected:
        try:
            target = safe_workspace_path(root, relative)
            with target.open("r", encoding="utf-8") as source:
                raw = source.read(_MAX_CONTEXT_FILE_CHARS + 1)
        except (OSError, UnicodeDecodeError, ActionRejected, WorkspaceSandboxError):
            continue
        content = safe_excerpt(raw[:_MAX_CONTEXT_FILE_CHARS], _MAX_CONTEXT_FILE_CHARS)
        if len(raw) > _MAX_CONTEXT_FILE_CHARS:
            content += "\n[truncated]"
        contents.append({"path": relative, "content": content})
    return tree, contents


def _safe_text(value: Any, limit: int = 4_000) -> str:
    text = safe_excerpt(str(value or ""), limit)
    if _SECRET_WORDS.search(text):
        sanitized = sanitize_untrusted_content(text, source="remote_worker_action")
        return sanitized["quarantined_text"][:limit]
    return text


class RemoteAgentLoop:
    """Iterative remote-worker action loop sharing AgentLoop's protocol and sandbox policy."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        permissions: list[str] | None,
        model_call: ModelCall,
        progress_reporter: ProgressReporter,
        tool_runner: ToolRunner,
        max_iterations: int = 8,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be positive")
        self.workspace_root = Path(workspace_root).resolve()
        self.permissions = permissions or []
        self.model_call = model_call
        self.progress_reporter = progress_reporter
        self.tool_runner = tool_runner
        self.max_iterations = max_iterations

    async def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        correction: str | None = None
        task_id = str(task.get("id") or "")
        expected = list(task.get("expected_deliverables") or ["deliverable_report.md"])

        for iteration in range(1, self.max_iterations + 1):
            tree, file_contents = _workspace_context(self.workspace_root, task)
            context = {
                "workspace_file_tree": tree,
                "relevant_workspace_file_contents": file_contents,
                "previous_iterations": history[-8:],
                "last_correction": correction,
            }
            raw_response = await self.model_call(context)
            try:
                action = parse_action_response(raw_response)
            except ActionProtocolError as exc:
                correction = f"Invalid action response: {exc} Return one JSON object using only the remote action protocol."
                detail = safe_excerpt(correction, 1_000)
                entry = self._trace(iteration, "invalid", "rejected", 1, detail, 0.0)
                trace.append(entry)
                history.append({"iteration": iteration, "action": "invalid", "result": detail, "correction": detail})
                await self._progress(
                    task_id,
                    iteration,
                    "agent_action_rejected",
                    detail,
                    None,
                )
                continue

            started = time.perf_counter()
            try:
                result, completed, artifact, actual_deliverables = await self._execute_action(
                    task, action, artifacts, expected
                )
            except (ActionRejected, WorkspaceSandboxError) as exc:
                correction = f"Action rejected by the device workspace safety policy: {exc}"
                detail = safe_excerpt(correction, 1_000)
                entry = self._trace(
                    iteration,
                    action.action,
                    "rejected",
                    1,
                    detail,
                    (time.perf_counter() - started) * 1000,
                )
                trace.append(entry)
                history.append({"iteration": iteration, "action": action.action, "result": detail, "correction": detail})
                await self._progress(
                    task_id,
                    iteration,
                    "agent_action_rejected",
                    detail,
                    None,
                )
                continue

            detail = safe_excerpt(result, 4_000)
            exit_code = 0
            status = "completed"
            if isinstance(action, RunAction) and "Command exit code: 0" not in detail:
                match = re.search(r"Command exit code: (-?\d+)", detail)
                if match and int(match.group(1)) != 0:
                    exit_code = int(match.group(1))
                    status = "failed"
            trace.append(
                self._trace(
                    iteration,
                    action.action,
                    status,
                    exit_code,
                    detail,
                    (time.perf_counter() - started) * 1000,
                )
            )
            history.append({"iteration": iteration, "action": action.action, "result": detail})
            if artifact is not None:
                artifacts.append(artifact)
            correction = None
            await self._progress(
                task_id,
                iteration,
                "agent_action",
                detail,
                artifact,
            )
            if completed:
                return {
                    "success": True,
                    "summary": detail,
                    "artifacts": artifacts,
                    "trace": trace,
                    "actual_deliverables": actual_deliverables,
                    "error": None,
                }

        reason = f"Agent iteration limit reached ({self.max_iterations}) before verified completion."
        return {
            "success": False,
            "summary": reason,
            "error": reason,
            "artifacts": artifacts,
            "trace": trace,
            "actual_deliverables": [],
        }

    async def _execute_action(
        self,
        task: dict[str, Any],
        action: Any,
        artifacts: list[dict[str, Any]],
        expected_deliverables: list[str],
    ) -> tuple[str, bool, dict[str, Any] | None, list[str]]:
        if isinstance(action, ReadAction):
            target = safe_workspace_path(self.workspace_root, action.path)
            if not target.is_file():
                raise ActionRejected(f"File '{action.path}' does not exist in the device workspace.")
            try:
                with target.open("r", encoding="utf-8") as source:
                    content = source.read(MAX_READ_CHARS + 1)
            except UnicodeDecodeError as exc:
                raise ActionRejected("Only UTF-8 text files may be read through the action protocol.") from exc
            truncated = len(content) > MAX_READ_CHARS
            content = safe_excerpt(content[:MAX_READ_CHARS], MAX_READ_CHARS)
            if truncated:
                content += "\n[truncated]"
            artifact = self._artifact(action.path, "read", 0, f"Read up to {len(content)} characters from a workspace text file.")
            return f"Read workspace file {action.path}:\n{content}", False, artifact, []

        if isinstance(action, WriteAction):
            if not can_write_permissions(self.permissions):
                raise ActionRejected("This remote employee does not have permission to edit workspace files.")
            target = safe_workspace_path(self.workspace_root, action.path, for_write=True)
            if contains_credentials(action.content):
                raise ActionRejected("Content that appears to contain credentials or private keys cannot be written.")
            existed = target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.content, encoding="utf-8")
            operation = "modify" if existed else "create"
            artifact = self._artifact(action.path, operation, 0, f"Wrote {len(action.content)} characters to {action.path}.")
            return f"Wrote {len(action.content)} characters to {action.path}.", False, artifact, []

        if isinstance(action, RunAction):
            if not can_run_permissions(self.permissions):
                raise ActionRejected("This remote employee does not have permission to run test or lint tools.")
            command = validate_run_command(self.workspace_root, action.command)
            result = await self.tool_runner(command)
            exit_code = int(result["exit_code"])
            output = (
                f"Command exit code: {exit_code}\n"
                f"stdout:\n{safe_excerpt(result.get('stdout'))}\n"
                f"stderr:\n{safe_excerpt(result.get('stderr'))}"
            )
            artifact = self._artifact(
                ".",
                "test",
                exit_code,
                safe_excerpt(result.get("stdout") or result.get("stderr") or "Workspace command executed.", 1_000),
            )
            return output, False, artifact, []

        if isinstance(action, DoneAction):
            declared = list(dict.fromkeys([*expected_deliverables, *action.deliverables]))
            if not declared:
                declared = list(
                    dict.fromkeys(
                        artifact["relative_path"]
                        for artifact in artifacts
                        if artifact.get("action") in {"create", "modify"}
                    )
                )
            verified: list[str] = []
            missing: list[str] = []
            for relative in declared:
                try:
                    target = safe_workspace_path(self.workspace_root, relative)
                except (ActionRejected, WorkspaceSandboxError):
                    missing.append(relative)
                    continue
                if target.is_file():
                    verified.append(relative)
                else:
                    missing.append(relative)
            if missing:
                return (
                    "Completion not verified. Missing or unsafe declared deliverables: "
                    + ", ".join(missing[:20])
                    + ". Create the files inside the device workspace, then return done again.",
                    False,
                    None,
                    [],
                )

            test_files = self._existing_test_files()
            if test_files:
                command = validate_run_command(self.workspace_root, ["pytest", "-q", *test_files])
                test_result = await self.tool_runner(command)
                test_exit = int(test_result["exit_code"])
                test_artifact = self._artifact(
                    ".",
                    "test",
                    test_exit,
                    safe_excerpt(test_result.get("stdout") or test_result.get("stderr") or "Final tests executed.", 1_000),
                )
                await self._progress(
                    str(task.get("id") or ""),
                    self.max_iterations,
                    "final_test_verification",
                    f"Final workspace pytest exit code: {test_exit}.",
                    test_artifact,
                )
                artifacts.append(test_artifact)
                if test_exit != 0:
                    return (
                        "Final workspace test verification failed: "
                        + safe_excerpt(test_result.get("stdout"))
                        + " "
                        + safe_excerpt(test_result.get("stderr")),
                        False,
                        None,
                        [],
                    )
            expected_count = len(verified)
            if test_files:
                summary = (
                    f"{action.summary} Verified {expected_count} deliverable(s) and passed "
                    f"{len(test_files)} workspace test file(s)."
                )
            else:
                summary = f"{action.summary} Verified {expected_count} deliverable(s); no workspace test files were present."
            return summary, True, None, verified

        if isinstance(action, (MessageAction, ApprovalAction)):
            raise ActionRejected("Collaboration and approval actions are not executable by a remote worker. Request local review.")
        raise ActionRejected("Action type is not supported by the remote worker.")

    def _existing_test_files(self) -> list[str]:
        files: list[str] = []
        for item in sorted(self.workspace_root.rglob("*.py")):
            relative = item.relative_to(self.workspace_root).as_posix()
            if item.is_symlink() or is_sensitive_workspace_path(relative):
                continue
            if not (item.name.startswith("test_") or item.name.endswith("_test.py")):
                continue
            try:
                target = safe_workspace_path(self.workspace_root, relative)
            except (ActionRejected, WorkspaceSandboxError):
                continue
            if target.is_file():
                files.append(relative)
        return files

    @staticmethod
    def _artifact(relative_path: str, action: str, exit_code: int, summary: str) -> dict[str, Any]:
        return {
            "relative_path": relative_path,
            "action": action,
            "exit_code": exit_code,
            "summary": safe_excerpt(summary, 1_000),
        }

    async def _progress(
        self,
        task_id: str,
        iteration: int,
        event: str,
        summary: str,
        artifact: dict[str, Any] | None,
    ) -> None:
        progress = min(95, max(1, int(iteration / self.max_iterations * 90)))
        await self.progress_reporter(
            {
                "progress_percent": progress,
                "event": event,
                "summary": _safe_text(summary),
                "artifact": artifact,
                "task_id": task_id,
                "iteration": iteration,
            }
        )

    @staticmethod
    def _trace(
        iteration: int,
        action: str,
        status: str,
        exit_code: int,
        detail: str,
        elapsed_ms: float,
    ) -> dict[str, Any]:
        return {
            "step": f"agent_iteration_{iteration}",
            "action": action,
            "status": status,
            "exit_code": exit_code,
            "elapsed_ms": round(elapsed_ms, 2),
            "detail": safe_excerpt(detail, 1_000),
        }
