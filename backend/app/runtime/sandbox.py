"""Shared workspace-containment and OS/container sandbox primitives.

Both the server-side `AgentRuntimeExecutor` and the remote `WorkerDaemon` rely on
these helpers so path containment, allowlisted binary validation, secret-scrubbed
subprocess environments, POSIX `rlimit` bounds, and optional hardened Docker
execution stay consistent across local and remote execution nodes.
"""

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from app.core.config import get_settings

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX platforms (e.g., Windows)
    resource = None  # type: ignore[assignment]

# Tool binaries an agent/worker is allowed to execute inside an isolated workspace.
ALLOWED_WORKSPACE_BINARIES: frozenset[str] = frozenset(
    {"python3", "python", "pytest", "ruff", "ls", "cat"}
)

ALLOWED_SANDBOX_MODES: frozenset[str] = frozenset({"process", "docker"})
DEFAULT_DOCKER_IMAGE = "aatm-sandbox:latest"

# Strictly allowlisted non-sensitive host environment variables passed to child tools.
SAFE_HOST_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "HOME",
    "PYTHONDONTWRITEBYTECODE",
)

# Explicit secret variable names and case-insensitive substrings blocked even in `extra`.
SENSITIVE_ENV_EXACT: frozenset[str] = frozenset(
    {
        "SECRET_KEY",
        "ANTHROPIC_API_KEY",
        "ARENA_BRIDGE_TOKEN",
        "ARENA_BRIDGE_UPSTREAM_API_KEY",
        "AATM_GITHUB_WEBHOOK_SECRET",
        "AATM_DEVICE_TOKEN",
        "AATM_WORKER_TOKEN",
        "DEFAULT_ADMIN_PASSWORD",
        "DATABASE_URL",
        "SYNC_DATABASE_URL",
    }
)
SENSITIVE_ENV_SUBSTRINGS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "KEY",
    "CREDENTIAL",
    "COOKIE",
    "PRIVATE",
    "AUTH",
)

SENSITIVE_WORKSPACE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".ssh",
        ".aws",
        ".azure",
        ".kube",
        ".gnupg",
        ".docker",
        ".terraform",
        "secrets",
        "credentials",
        "credential",
        "private_keys",
        "private-keys",
    }
)
SENSITIVE_WORKSPACE_FILES: frozenset[str] = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".pgpass",
        ".git-credentials",
        ".gitconfig",
        "authorized_keys",
        "known_hosts",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials.json",
        "secrets.json",
        "service-account.json",
        "terraform.tfstate",
        "terraform.tfstate.backup",
    }
)
SENSITIVE_WORKSPACE_SUFFIXES: tuple[str, ...] = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".kdbx",
    ".tfstate",
    ".tfstate.backup",
    ".secret",
    ".secrets",
    ".credential",
    ".credentials",
    ".token",
    ".password",
)


class WorkspaceSandboxError(PermissionError):
    """Raised when an operation would escape its isolated workspace, use a denied tool, or fail sandbox policy."""


def is_sensitive_workspace_path(relative_path: str) -> bool:
    """Return True for credential stores and repository-control metadata.

    The check is case-insensitive and examines every path component, so a secret
    file cannot be exposed merely by nesting it below an otherwise ordinary folder.
    """
    if not isinstance(relative_path, str):
        return True
    normalized = relative_path.strip().replace("\\", "/")
    if not normalized or "\x00" in normalized or normalized.startswith("/") or PureWindowsPath(normalized).drive:
        return True

    parts = [part.lower() for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        return True
    if any(part in SENSITIVE_WORKSPACE_DIRS for part in parts):
        return True
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return True

    basename = parts[-1]
    if basename in SENSITIVE_WORKSPACE_FILES:
        return True
    return basename.endswith(SENSITIVE_WORKSPACE_SUFFIXES)


@dataclass(frozen=True)
class SandboxExecutionPolicy:
    """Resource and isolation policy for sandboxed tool execution."""

    sandbox_mode: str = "docker"
    memory_mb: int = 512
    cpu_seconds: int = 30
    cpu_limit: float = 1.0
    max_procs: int = 64
    docker_image: str = DEFAULT_DOCKER_IMAGE
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        norm_mode = (self.sandbox_mode or "docker").strip().lower()
        if norm_mode not in ALLOWED_SANDBOX_MODES:
            raise WorkspaceSandboxError(
                f"Unsupported sandbox mode '{self.sandbox_mode}'. Allowed: {sorted(ALLOWED_SANDBOX_MODES)}."
            )
        if self.memory_mb < 64:
            raise WorkspaceSandboxError("Sandbox memory_mb must be at least 64 MB.")
        if self.cpu_seconds < 1:
            raise WorkspaceSandboxError("Sandbox cpu_seconds must be at least 1 second.")
        if self.cpu_limit <= 0:
            raise WorkspaceSandboxError("Sandbox cpu_limit must be positive.")
        if self.max_procs < 8:
            raise WorkspaceSandboxError("Sandbox max_procs / pids_limit must be at least 8.")

    @classmethod
    def from_env(cls, *, timeout_seconds: int = 20) -> "SandboxExecutionPolicy":
        mode = os.environ.get("AATM_SANDBOX_MODE", "docker").strip().lower() or "docker"
        memory_mb = int(os.environ.get("AATM_SANDBOX_MEMORY_MB", "512"))
        cpu_seconds = int(os.environ.get("AATM_SANDBOX_CPU_SECONDS", "30"))
        cpu_limit = float(os.environ.get("AATM_SANDBOX_CPU_LIMIT", "1.0"))
        max_procs = int(os.environ.get("AATM_SANDBOX_PIDS_LIMIT", "64"))
        docker_image = (
            os.environ.get("AATM_SANDBOX_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE).strip()
            or DEFAULT_DOCKER_IMAGE
        )
        return cls(
            sandbox_mode=mode,
            memory_mb=memory_mb,
            cpu_seconds=cpu_seconds,
            cpu_limit=cpu_limit,
            max_procs=max_procs,
            docker_image=docker_image,
            timeout_seconds=timeout_seconds,
        )


def is_sensitive_env_key(name: str) -> bool:
    """Return True if an environment variable name could carry secrets, tokens, or credentials."""
    upper = name.strip().upper()
    if not upper:
        return True
    if upper in SENSITIVE_ENV_EXACT:
        return True
    return any(marker in upper for marker in SENSITIVE_ENV_SUBSTRINGS)


def build_sanitized_env(
    extra: Mapping[str, str] | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal subprocess environment stripped of all host secrets and tokens.

    Only allowlisted safe variables (`PATH`, `LANG`, `LC_ALL`, `HOME`,
    `PYTHONDONTWRITEBYTECODE`) are forwarded from the host environment. Sensitive
    names (`SECRET_KEY`, `ANTHROPIC_API_KEY`, `ARENA_BRIDGE_TOKEN`,
    `AATM_GITHUB_WEBHOOK_SECRET`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*KEY*`, etc.)
    are excluded from both the host environment and any caller-supplied `extra` mapping.
    """
    source = os.environ if base_env is None else base_env
    sanitized: dict[str, str] = {}
    for key in SAFE_HOST_ENV_KEYS:
        val = source.get(key)
        if val is not None:
            sanitized[key] = str(val)

    sanitized.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    if extra:
        for key, val in extra.items():
            if not key or is_sensitive_env_key(key):
                continue
            sanitized[str(key)] = str(val)
    return sanitized


def build_resource_preexec(
    memory_mb: int = 512,
    cpu_seconds: int = 30,
    max_procs: int = 64,
) -> Callable[[], None] | None:
    """Return a POSIX `preexec_fn` applying best-effort `resource.setrlimit` caps.

    Returns `None` on non-POSIX platforms where `resource` is unavailable.
    """
    if resource is None or os.name != "posix":
        return None

    mem_bytes = max(64, int(memory_mb)) * 1024 * 1024
    cpu_cap = max(1, int(cpu_seconds))
    proc_cap = max(8, int(max_procs))

    def _apply_limits() -> None:
        limits: list[tuple[int, int]] = []
        if hasattr(resource, "RLIMIT_CPU"):
            limits.append((resource.RLIMIT_CPU, cpu_cap))
        if hasattr(resource, "RLIMIT_AS"):
            limits.append((resource.RLIMIT_AS, mem_bytes))
        if hasattr(resource, "RLIMIT_NPROC"):
            limits.append((resource.RLIMIT_NPROC, proc_cap))

        for lim_const, desired in limits:
            try:
                _soft, hard = resource.getrlimit(lim_const)
                target = desired if hard == resource.RLIM_INFINITY else min(desired, hard)
                resource.setrlimit(lim_const, (target, target))
            except (ValueError, OSError):
                # Best-effort: restricted containers or macOS may reject specific rlimits.
                continue

    return _apply_limits


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


def build_isolated_command(
    command: list[str],
    workspace: Path | str,
    *,
    sandbox_mode: str | None = None,
    memory_mb: int = 512,
    cpu_limit: float = 1.0,
    pids_limit: int = 64,
    docker_image: str = DEFAULT_DOCKER_IMAGE,
    docker_bin: str | None = None,
) -> list[str]:
    """Build the execution command for `process` or hardened `docker` (default) sandbox mode.

    Always validates `command` against `ALLOWED_WORKSPACE_BINARIES`. In `docker` mode,
    wraps the command in a non-networked, read-only, capability-dropped container mounted
    strictly to `workspace` with `--pull=never` and fails closed with `WorkspaceSandboxError`
    if the `docker` CLI binary is unavailable.
    """
    exe = assert_allowed_binary(command)
    mode = (
        sandbox_mode
        if sandbox_mode is not None
        else os.environ.get("AATM_SANDBOX_MODE", "docker")
    ).strip().lower() or "docker"

    if mode not in ALLOWED_SANDBOX_MODES:
        raise WorkspaceSandboxError(
            f"Unsupported sandbox mode '{mode}'. Allowed: {sorted(ALLOWED_SANDBOX_MODES)}."
        )

    ws = Path(workspace).resolve()
    if mode == "process":
        return list(command)

    resolved_docker = docker_bin if docker_bin is not None else shutil.which("docker")
    if not resolved_docker:
        raise WorkspaceSandboxError(
            "Docker sandbox mode requested, but 'docker' CLI binary was not found on PATH; failing closed."
        )

    mem_cap = max(64, int(memory_mb))
    cpu_cap = max(0.1, float(cpu_limit))
    pid_cap = max(8, int(pids_limit))
    image = (docker_image or DEFAULT_DOCKER_IMAGE).strip() or DEFAULT_DOCKER_IMAGE
    # Inside the container image, invoke the bare allowlisted tool rather than a host virtualenv path.
    container_cmd = [exe, *command[1:]]

    return [
        resolved_docker,
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--pids-limit={pid_cap}",
        f"--memory={mem_cap}m",
        f"--cpus={cpu_cap:g}",
        "-v",
        f"{ws}:/workspace:rw",
        "-w",
        "/workspace",
        image,
        *container_cmd,
    ]


def get_sandbox_status() -> dict[str, Any]:
    """Inspect sandbox availability without ever falling back silently to process mode."""
    settings = get_settings()
    mode = (
        os.environ.get("AATM_SANDBOX_MODE")
        or os.environ.get("SANDBOX_MODE")
        or settings.AATM_SANDBOX_MODE
        or "docker"
    ).strip().lower()
    image = (
        os.environ.get("AATM_SANDBOX_DOCKER_IMAGE")
        or settings.SANDBOX_DOCKER_IMAGE
        or DEFAULT_DOCKER_IMAGE
    ).strip() or DEFAULT_DOCKER_IMAGE

    if mode not in ALLOWED_SANDBOX_MODES:
        return {
            "status": "SANDBOX_UNAVAILABLE",
            "available": False,
            "mode": mode,
            "image": image,
            "reason": f"Unsupported sandbox mode '{mode}'.",
        }

    if mode == "process":
        allow_env = os.environ.get("AATM_ALLOW_PROCESS_SANDBOX", "").strip().lower()
        allow_process = allow_env in {"1", "true", "yes"} or bool(settings.AATM_ALLOW_PROCESS_SANDBOX)
        env_name = (os.environ.get("ENVIRONMENT") or settings.ENVIRONMENT or "").strip().lower()
        if env_name == "production" and not allow_process:
            return {
                "status": "SANDBOX_UNAVAILABLE",
                "available": False,
                "mode": "process",
                "image": None,
                "reason": "Process sandbox mode is disabled in production unless AATM_ALLOW_PROCESS_SANDBOX=true.",
            }
        return {
            "status": "READY",
            "available": True,
            "mode": "process",
            "image": None,
            "reason": None,
        }

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return {
            "status": "SANDBOX_UNAVAILABLE",
            "available": False,
            "mode": "docker",
            "image": image,
            "reason": "Docker CLI is not installed or not on PATH; docker sandbox cannot run.",
        }

    try:
        proc = subprocess.run(
            [docker_bin, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "status": "SANDBOX_UNAVAILABLE",
            "available": False,
            "mode": "docker",
            "image": image,
            "reason": f"Failed to inspect sandbox image '{image}': {exc}",
        }

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        reason = f"Sandbox image '{image}' is missing or Docker daemon is unreachable."
        if detail:
            reason = f"{reason} ({detail.splitlines()[0][:180]})"
        return {
            "status": "SANDBOX_UNAVAILABLE",
            "available": False,
            "mode": "docker",
            "image": image,
            "reason": reason,
        }

    return {
        "status": "READY",
        "available": True,
        "mode": "docker",
        "image": image,
        "reason": None,
    }
