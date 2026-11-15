"""Shared workspace-containment primitives.

Both the server-side `AgentRuntimeExecutor` and the remote `WorkerDaemon` need the
exact same containment guarantees, so the rules live here instead of being
duplicated (and drifting) across the two call sites.
"""

from pathlib import Path

# Tool binaries an agent/worker is allowed to execute inside an isolated workspace.
ALLOWED_WORKSPACE_BINARIES: frozenset[str] = frozenset(
    {"python3", "python", "pytest", "ruff", "ls", "cat"}
)


class WorkspaceSandboxError(PermissionError):
    """Raised when an operation would escape its isolated workspace or use a denied tool."""


def resolve_contained_path(root: Path, relative_path: str) -> Path:
    """Resolve `relative_path` inside `root`, refusing anything that escapes the sandbox.

    Blocks relative traversal (`../../etc/passwd`), absolute paths (`/etc/passwd`)
    and symlink escapes (the candidate is fully resolved before containment is
    checked, so a symlink pointing outside the workspace is rejected too).
    """
    root = Path(root).resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise WorkspaceSandboxError(
            f"Access denied: path '{relative_path}' escapes isolated workspace '{root.name}'."
        )
    return candidate


def assert_allowed_binary(command: list[str]) -> str:
    """Validate that `command` invokes an allowlisted development tool.

    Returns the resolved binary name. Raises `WorkspaceSandboxError` otherwise.
    """
    if not command:
        raise WorkspaceSandboxError("Command must not be empty.")
    exe = Path(command[0]).name
    if exe not in ALLOWED_WORKSPACE_BINARIES:
        raise WorkspaceSandboxError(
            f"Command '{command[0]}' not in allowed workspace toolset "
            f"{sorted(ALLOWED_WORKSPACE_BINARIES)}."
        )
    return exe
