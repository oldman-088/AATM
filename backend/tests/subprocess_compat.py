"""Test helper: route legacy synchronous ``subprocess.run`` fakes through the async ``run_exec`` seam.

Production code now uses ``app.core.async_subprocess.run_exec`` (asyncio.create_subprocess_exec)
in every async path. Existing tests that faked ``subprocess.run`` keep their fake bodies and
assertions unchanged; this adapter calls the fake with the same arguments and converts the
``CompletedProcess`` it returns.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from app.core.async_subprocess import CommandResult


def patch_run_exec(monkeypatch: pytest.MonkeyPatch, module: str, fake_run: Callable[..., Any]) -> None:
    async def _run_exec(argv, *, cwd=None, env=None, timeout=30.0, preexec_fn=None):
        try:
            proc = fake_run(list(argv), cwd=str(cwd) if cwd is not None else None, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"Command timed out after {timeout:g}s.", timed_out=True)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode()
        if isinstance(stderr, bytes):
            stderr = stderr.decode()
        return CommandResult(int(proc.returncode), stdout, stderr)

    monkeypatch.setattr(f"{module}.run_exec", _run_exec)
