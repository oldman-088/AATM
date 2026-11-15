"""AATM remote worker daemon.

Runs on a separate execution host, registers itself with the central AATM server,
heartbeats, claims tasks over HTTP and executes them inside an isolated local
workspace before reporting the result back.

Usage:
    python -m app.runtime.worker_daemon \\
        --server-url http://aatm-server:8000 \\
        --device-id node-win11-01 \\
        --token <device token>

Configuration can also be supplied through the environment:
    AATM_SERVER_URL, AATM_DEVICE_ID, AATM_DEVICE_TOKEN, AATM_WORKER_WORKSPACE,
    AATM_WORKER_POLL_SECONDS, AATM_WORKER_MAX_CONCURRENT, AATM_WORKER_TOKEN
"""

import argparse
import asyncio
import json
import logging
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.runtime.sandbox import (
    WorkspaceSandboxError,
    assert_allowed_binary,
    resolve_contained_path,
)

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_COMMAND_TIMEOUT = 120


@dataclass
class WorkerConfig:
    """Runtime configuration for a remote worker daemon."""

    server_url: str = "http://127.0.0.1:8000"
    device_id: str = "device-remote"
    device_name: str = ""
    token: str = ""
    workspace_root: Path = field(default_factory=lambda: Path("data/remote_workspaces"))
    poll_seconds: float = DEFAULT_POLL_SECONDS
    max_concurrent: int = 2
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT
    device_type: str = "remote_server"

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        root = Path(os.environ.get("AATM_WORKER_WORKSPACE", "data/remote_workspaces"))
        device_id = os.environ.get("AATM_DEVICE_ID", "device-remote")
        return cls(
            server_url=os.environ.get("AATM_SERVER_URL", "http://127.0.0.1:8000").rstrip("/"),
            device_id=device_id,
            device_name=os.environ.get("AATM_DEVICE_NAME", f"Remote worker {device_id}"),
            token=os.environ.get("AATM_DEVICE_TOKEN", os.environ.get("AATM_WORKER_TOKEN", "")),
            workspace_root=root / device_id,
            poll_seconds=float(os.environ.get("AATM_WORKER_POLL_SECONDS", DEFAULT_POLL_SECONDS)),
            max_concurrent=int(os.environ.get("AATM_WORKER_MAX_CONCURRENT", 2)),
            device_type=os.environ.get("AATM_DEVICE_TYPE", "remote_server"),
        )


class WorkerDaemon:
    """Registers with the central server and executes claimed tasks locally."""

    def __init__(self, config: WorkerConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        self.worker_id = f"{config.device_id}-worker"
        self.token_revoked: bool = False
        self.stop_reason: str | None = None
        self.cycles_completed: int = 0
        if client is not None:
            client.headers.update(self._auth_headers())

    def _record_token_revocation(self, action: str) -> None:
        """Record and emit an explicit token-revocation notice once per 401 event."""
        if not self.token_revoked:
            self.token_revoked = True
            self.stop_reason = (
                f"Device token revoked or rejected (HTTP 401) on {action} for device '{self.config.device_id}'."
            )
            notice = (
                f"[aatm-worker] TOKEN REVOKED (HTTP 401) during {action} for device={self.config.device_id}. "
                "Stopping polling cleanly — issue a new device token and restart the worker daemon."
            )
            logger.error(notice)
            print(notice, flush=True)

    def _check_response(self, action: str, resp: httpx.Response) -> None:
        if resp.status_code == 401:
            self._record_token_revocation(action)
        resp.raise_for_status()

    # ------------------------------------------------------------------ plumbing
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.server_url,
                timeout=30.0,
                headers=self._auth_headers(),
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        return {"X-AATM-Device-Token": self.config.token} if self.config.token else {}

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ workspace
    @property
    def workspace_root(self) -> Path:
        root = self.config.workspace_root
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def resolve_path(self, relative_path: str) -> Path:
        """Resolve a path inside this device's isolated workspace."""
        return resolve_contained_path(self.workspace_root, relative_path)

    def run_tool(self, command: list[str]) -> dict[str, Any]:
        """Execute an allowlisted development tool inside the isolated workspace."""
        assert_allowed_binary(command)
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                command,
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=self.config.command_timeout,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-8000:],
                "stderr": proc.stderr[-4000:],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        except subprocess.TimeoutExpired:
            return {
                "exit_code": 124,
                "stdout": "",
                "stderr": f"Command timed out after {self.config.command_timeout}s.",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }

    # ------------------------------------------------------------------ server calls
    async def register(self) -> dict[str, Any]:
        payload = {
            "name": self.config.device_name or f"Remote worker {self.config.device_id}",
            "device_type": self.config.device_type,
            "host_address": platform.node(),
            "os_name": f"{platform.system()} {platform.release()}",
            "cpu_cores": os.cpu_count() or 1,
            "capabilities": ["python3", "pytest", "ruff", "workspace_exec"],
            "max_concurrent_tasks": self.config.max_concurrent,
            "workspace_root": str(self.workspace_root),
            "worker_id": self.worker_id,
        }
        resp = await self.client.post(f"/api/v1/devices/{self.config.device_id}/register", json=payload)
        self._check_response("register", resp)
        return resp.json()

    async def heartbeat(self) -> dict[str, Any]:
        resp = await self.client.post(f"/api/v1/devices/{self.config.device_id}/heartbeat")
        self._check_response("heartbeat", resp)
        return resp.json()

    async def claim(self) -> dict[str, Any] | None:
        resp = await self.client.post(
            f"/api/v1/devices/{self.config.device_id}/claim",
            json={"worker_id": self.worker_id, "max_concurrent": self.config.max_concurrent},
        )
        self._check_response("claim", resp)
        return resp.json().get("task")

    async def report(
        self,
        task_id: str,
        success: bool,
        summary: str,
        artifacts: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        resp = await self.client.post(
            f"/api/v1/devices/{self.config.device_id}/tasks/{task_id}/report",
            json={
                "worker_id": self.worker_id,
                "success": success,
                "summary": summary,
                "artifacts": artifacts or [],
                "error_message": error,
            },
        )
        self._check_response("report", resp)
        return resp.json()

    # ------------------------------------------------------------------ execution
    async def execute_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Execute a claimed task inside the isolated workspace and build the report."""
        title = task.get("title", "Untitled task")
        description = task.get("description", "")
        deliverables = task.get("expected_deliverables") or ["deliverable_report.md"]
        artifacts: list[dict[str, Any]] = []
        steps: list[str] = []

        # 1. Write the primary deliverable.
        primary = deliverables[0]
        try:
            target = self.resolve_path(primary)
        except WorkspaceSandboxError as exc:
            return {"success": False, "summary": f"Rejected unsafe deliverable path: {exc}", "artifacts": []}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"# {title}\n\n"
            f"## Instructions\n{description or '(none provided)'}\n\n"
            f"## Execution\n"
            f"- Device: {self.config.device_id}\n"
            f"- Worker: {self.worker_id}\n"
            f"- Workspace: {self.workspace_root}\n"
            f"- Host: {platform.node()} ({platform.system()} {platform.release()})\n",
            encoding="utf-8",
        )
        artifacts.append({"relative_path": primary, "action": "create", "exit_code": 0})
        steps.append(f"Wrote deliverable '{primary}'")

        # 2. Run the project test-suite if one is present, so the report carries evidence.
        if (self.workspace_root / "pytest.ini").exists() or any(self.workspace_root.glob("test_*.py")):
            test_result = self.run_tool(["pytest", "-q"])
            artifacts.append(
                {"relative_path": ".", "action": "test", "exit_code": test_result["exit_code"]}
            )
            steps.append(f"pytest exit={test_result['exit_code']}")

        summary = "; ".join(steps) or "Deliverable written"
        return {"success": True, "summary": summary, "artifacts": artifacts}

    # ------------------------------------------------------------------ main loop
    async def run_once(self) -> dict[str, Any] | None:
        """One heartbeat/claim/execute/report cycle.

        Returns the executed task payload, or None when there was no work to do.
        """
        await self.heartbeat()
        task = await self.claim()
        if not task:
            return None
        try:
            result = await self.execute_task(task)
        except Exception as exc:  # noqa: BLE001 - the daemon must never die on one task
            result = {"success": False, "summary": "Worker raised an exception", "artifacts": [], "error": str(exc)}
        await self.report(
            task_id=task.get("id", ""),
            success=bool(result.get("success")),
            summary=str(result.get("summary", "")),
            artifacts=result.get("artifacts", []),
            error=result.get("error"),
        )
        return task

    async def run_forever(self, max_cycles: int | None = None) -> None:
        """Run the daemon loop until interrupted, token-revoked (HTTP 401), or `max_cycles` completed."""
        try:
            try:
                await self.register()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    self._record_token_revocation("register")
                    return
                raise

            cycles = 0
            while not self.token_revoked and (max_cycles is None or cycles < max_cycles):
                cycles += 1
                self.cycles_completed = cycles
                try:
                    task = await self.run_once()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 401:
                        self._record_token_revocation("poll")
                        break
                    print(f"[aatm-worker] server rejected request: {exc.response.status_code} {exc.response.text[:200]}")
                    task = None
                except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                    print(f"[aatm-worker] transient error: {exc}")
                    task = None
                if task is None and not self.token_revoked:
                    await asyncio.sleep(self.config.poll_seconds)
        finally:
            await self.aclose()


def _parse_args(argv: list[str] | None = None) -> tuple[WorkerConfig, int | None]:
    env = WorkerConfig.from_env()
    parser = argparse.ArgumentParser(description="AATM remote worker daemon")
    parser.add_argument("--server-url", default=env.server_url)
    parser.add_argument("--device-id", default=env.device_id)
    parser.add_argument("--device-name", default=env.device_name)
    parser.add_argument("--token", default=env.token)
    parser.add_argument("--workspace-root", default=str(env.workspace_root))
    parser.add_argument("--poll-seconds", type=float, default=env.poll_seconds)
    parser.add_argument("--max-concurrent", type=int, default=env.max_concurrent)
    parser.add_argument("--device-type", default=env.device_type)
    parser.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles (testing)")
    parser.add_argument("--print-config", action="store_true", help="Print the effective config and exit")
    args = parser.parse_args(argv)

    config = WorkerConfig(
        server_url=args.server_url.rstrip("/"),
        device_id=args.device_id,
        device_name=args.device_name,
        token=args.token,
        workspace_root=Path(args.workspace_root),
        poll_seconds=args.poll_seconds,
        max_concurrent=args.max_concurrent,
        device_type=args.device_type,
    )
    if args.print_config:
        redacted = json.dumps(
            {
                "server_url": config.server_url,
                "device_id": config.device_id,
                "token_configured": bool(config.token),
                "workspace_root": str(config.workspace_root),
                "poll_seconds": config.poll_seconds,
                "max_concurrent": config.max_concurrent,
            },
            indent=2,
        )
        print(redacted)
    return config, args.max_cycles


def main(argv: list[str] | None = None) -> int:
    config, max_cycles = _parse_args(argv)
    if not config.token:
        print(
            "[aatm-worker] ERROR: no device token configured. "
            "Set AATM_DEVICE_TOKEN (or pass --token).",
            flush=True,
        )
        return 2
    daemon = WorkerDaemon(config)
    print(
        f"[aatm-worker] starting: device={config.device_id} server={config.server_url} "
        f"workspace={config.workspace_root}",
        flush=True,
    )
    try:
        asyncio.run(daemon.run_forever(max_cycles=max_cycles))
    except KeyboardInterrupt:
        print("[aatm-worker] interrupted", flush=True)
    if daemon.token_revoked:
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
