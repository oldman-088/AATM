"""Non-blocking subprocess helper for async code paths (never ``subprocess.run`` inside ``async def``)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def output(self) -> str:
        """stdout, or stderr when stdout is empty (matches the old ``proc.stdout or proc.stderr``)."""
        return (self.stdout or self.stderr or "").strip()


async def run_exec(
    argv: list[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    preexec_fn: Callable[[], None] | None = None,
) -> CommandResult:
    """Run an argv-only command (no shell) with ``asyncio.create_subprocess_exec`` and a hard timeout.

    On timeout the process is killed and reaped; returncode 124 mirrors coreutils ``timeout``.
    """
    kwargs: dict = {}
    if preexec_fn is not None and os.name == "posix":
        kwargs["preexec_fn"] = preexec_fn
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except (OSError, ValueError) as exc:
        return CommandResult(127, "", str(exc))
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.communicate()
        return CommandResult(124, "", f"Command timed out after {timeout:g}s.", timed_out=True)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    return CommandResult(
        int(proc.returncode or 0),
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )
