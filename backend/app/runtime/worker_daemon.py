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
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.runtime.agent_protocol import validate_run_command
from app.runtime.remote_agent_loop import RemoteAgentLoop
from app.runtime.sandbox import (
    DEFAULT_DOCKER_IMAGE,
    WorkspaceSandboxError,
    assert_allowed_binary,
    build_isolated_command,
    build_resource_preexec,
    build_sanitized_env,
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
    keepalive_seconds: float | None = None
    max_concurrent: int = 2
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT
    max_iterations: int = 8
    device_type: str = "remote_server"
    sandbox_mode: str = field(
        default_factory=lambda: os.environ.get("AATM_SANDBOX_MODE", "docker").strip().lower() or "docker"
    )
    sandbox_memory_mb: int = 512
    sandbox_cpu_seconds: int = 30
    sandbox_cpu_limit: float = 1.0
    sandbox_pids_limit: int = 64
    sandbox_docker_image: str = DEFAULT_DOCKER_IMAGE

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        root = Path(os.environ.get("AATM_WORKER_WORKSPACE", "data/remote_workspaces"))
        device_id = os.environ.get("AATM_DEVICE_ID", "device-remote")
        raw_keepalive = os.environ.get("AATM_WORKER_KEEPALIVE_SECONDS", "").strip()
        keepalive = float(raw_keepalive) if raw_keepalive else None
        return cls(
            server_url=os.environ.get("AATM_SERVER_URL", "http://127.0.0.1:8000").rstrip("/"),
            device_id=device_id,
            device_name=os.environ.get("AATM_DEVICE_NAME", f"Remote worker {device_id}"),
            token=os.environ.get("AATM_DEVICE_TOKEN", os.environ.get("AATM_WORKER_TOKEN", "")),
            workspace_root=root / device_id,
            poll_seconds=float(os.environ.get("AATM_WORKER_POLL_SECONDS", DEFAULT_POLL_SECONDS)),
            keepalive_seconds=keepalive,
            max_concurrent=int(os.environ.get("AATM_WORKER_MAX_CONCURRENT", 2)),
            max_iterations=int(os.environ.get("AATM_WORKER_MAX_ITERATIONS", 8)),
            command_timeout=int(os.environ.get("AATM_WORKER_COMMAND_TIMEOUT", DEFAULT_COMMAND_TIMEOUT)),
            device_type=os.environ.get("AATM_DEVICE_TYPE", "remote_server"),
            sandbox_mode=os.environ.get("AATM_SANDBOX_MODE", "docker").strip().lower() or "docker",
            sandbox_memory_mb=int(os.environ.get("AATM_SANDBOX_MEMORY_MB", "512")),
            sandbox_cpu_seconds=int(os.environ.get("AATM_SANDBOX_CPU_SECONDS", "30")),
            sandbox_cpu_limit=float(os.environ.get("AATM_SANDBOX_CPU_LIMIT", "1.0")),
            sandbox_pids_limit=int(os.environ.get("AATM_SANDBOX_PIDS_LIMIT", "64")),
            sandbox_docker_image=(
                os.environ.get("AATM_SANDBOX_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE).strip()
                or DEFAULT_DOCKER_IMAGE
            ),
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
        self._revocation_error: httpx.HTTPStatusError | None = None
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
        configured_root = self.config.workspace_root
        if configured_root is None or not str(configured_root).strip():
            raise WorkspaceSandboxError("Remote execution requires a configured device workspace jail.")
        root = Path(configured_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
        if not root.is_dir() or root == Path(root.anchor):
            raise WorkspaceSandboxError("The configured device workspace jail must be a non-root directory.")
        return root

    def resolve_path(self, relative_path: str) -> Path:
        """Resolve a path inside this device's isolated workspace."""
        return resolve_contained_path(self.workspace_root, relative_path)

    def _run_tool(self, command: list[str], *, sandbox_mode: str | None = None) -> dict[str, Any]:
        """Execute an allowlisted tool inside the isolated workspace with scrubbed env and OS/Docker bounds."""
        exe = assert_allowed_binary(command)
        ws = self.workspace_root
        eff_mode = (sandbox_mode or self.config.sandbox_mode or "process").strip().lower() or "process"

        exec_command = list(command)
        if eff_mode == "process" and exe in ("python", "python3", "pytest", "ruff"):
            venv_tool = Path(sys.executable).parent / exe
            if venv_tool.exists():
                exec_command = [str(venv_tool), *command[1:]]

        isolated_cmd = build_isolated_command(
            exec_command,
            ws,
            sandbox_mode=eff_mode,
            memory_mb=self.config.sandbox_memory_mb,
            cpu_limit=self.config.sandbox_cpu_limit,
            pids_limit=self.config.sandbox_pids_limit,
            docker_image=self.config.sandbox_docker_image,
        )
        sanitized_env = build_sanitized_env({"HOME": str(ws)})
        preexec = (
            build_resource_preexec(
                memory_mb=self.config.sandbox_memory_mb,
                cpu_seconds=self.config.sandbox_cpu_seconds,
                max_procs=self.config.sandbox_pids_limit,
            )
            if eff_mode == "process"
            else None
        )

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                isolated_cmd,
                cwd=str(ws),
                env=sanitized_env,
                preexec_fn=preexec,
                capture_output=True,
                text=True,
                timeout=self.config.command_timeout,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-8000:],
                "stderr": proc.stderr[-4000:],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "sandbox_mode": eff_mode,
            }
        except subprocess.TimeoutExpired:
            return {
                "exit_code": 124,
                "stdout": "",
                "stderr": f"Command timed out after {self.config.command_timeout}s.",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "sandbox_mode": eff_mode,
            }

    def run_tool(self, command: list[str], *, sandbox_mode: str | None = None) -> dict[str, Any]:
        """Run a protocol-approved test/lint command inside the configured sandbox."""
        safe_command = validate_run_command(self.workspace_root, command)
        return self._run_tool(safe_command, sandbox_mode=sandbox_mode)

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

    async def request_model(self, task_id: str, context: dict[str, Any]) -> str:
        """Request one model turn from the central router; credentials stay on the server."""
        payload = {"worker_id": self.worker_id, **context}
        response = await self.client.post(
            f"/api/v1/devices/{self.config.device_id}/tasks/{task_id}/model",
            json=payload,
        )
        self._check_response("model", response)
        content = response.json().get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Central model endpoint returned no action response.")
        return content

    async def report_progress(
        self,
        task_id: str,
        *,
        progress_percent: int,
        event: str,
        summary: str,
        iteration: int = 0,
        artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "worker_id": self.worker_id,
            "progress_percent": progress_percent,
            "event": event,
            "summary": summary,
            "iteration": iteration,
            "artifact": artifact,
        }
        response = await self.client.post(
            f"/api/v1/devices/{self.config.device_id}/tasks/{task_id}/progress",
            json=payload,
        )
        self._check_response("progress", response)
        return response.json()

    async def report(
        self,
        task_id: str,
        success: bool,
        summary: str,
        artifacts: list[dict[str, Any]] | None = None,
        error: str | None = None,
        trace: list[dict[str, Any]] | None = None,
        actual_deliverables: list[str] | None = None,
        total_elapsed_ms: float | None = None,
        keepalive_heartbeats: int = 0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "worker_id": self.worker_id,
            "success": success,
            "summary": summary,
            "artifacts": artifacts or [],
            "actual_deliverables": actual_deliverables or [],
            "error_message": error,
            "trace": trace or [],
            "keepalive_heartbeats": keepalive_heartbeats,
        }
        if total_elapsed_ms is not None:
            payload["total_elapsed_ms"] = total_elapsed_ms
        resp = await self.client.post(
            f"/api/v1/devices/{self.config.device_id}/tasks/{task_id}/report",
            json=payload,
        )
        self._check_response("report", resp)
        return resp.json()

    # ------------------------------------------------------------------ execution
    def _effective_keepalive_seconds(self) -> float:
        if self.config.keepalive_seconds is not None and self.config.keepalive_seconds > 0:
            return float(self.config.keepalive_seconds)
        return min(max(0.5, float(self.config.poll_seconds)), 15.0)

    async def _run_task_steps(self, task: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
        """Run the structured remote AgentLoop within the device's sandboxed workspace."""
        root = self.workspace_root
        task_id = str(task.get("id") or "")
        if not task_id:
            raise ValueError("A remote AgentLoop task requires a central task id.")

        async def model_call(context: dict[str, Any]) -> str:
            return await self.request_model(task_id, context)

        async def progress_reporter(event: dict[str, Any]) -> None:
            await self.report_progress(
                task_id,
                progress_percent=int(event.get("progress_percent", 0)),
                event=str(event.get("event", "agent_action")),
                summary=str(event.get("summary", "")),
                iteration=int(event.get("iteration", 0)),
                artifact=event.get("artifact"),
            )

        employee = task.get("employee") if isinstance(task.get("employee"), dict) else {}
        loop = RemoteAgentLoop(
            workspace_root=root,
            permissions=list(employee.get("permissions") or []),
            model_call=model_call,
            progress_reporter=progress_reporter,
            tool_runner=self.run_tool,
            max_iterations=self.config.max_iterations,
        )
        result = await loop.execute(task)
        trace.extend(result.get("trace", []))
        return result

    async def execute_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Execute a claimed task inside the isolated workspace with an in-flight heartbeat keepalive loop."""
        _ = self.workspace_root  # Fail closed before creating model, progress, or subprocess work.
        exec_t0 = time.perf_counter()
        trace: list[dict[str, Any]] = []
        keepalive_heartbeats = 0
        keepalive_interval = self._effective_keepalive_seconds()
        keepalive_401: httpx.HTTPStatusError | None = None

        async def _keepalive_loop() -> None:
            nonlocal keepalive_heartbeats, keepalive_401
            while True:
                await asyncio.sleep(keepalive_interval)
                try:
                    await self.heartbeat()
                    keepalive_heartbeats += 1
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 401:
                        keepalive_401 = exc
                        self._revocation_error = exc
                        self._record_token_revocation("heartbeat")
                        return
                    logger.warning(
                        "[aatm-worker] keepalive heartbeat returned HTTP %s for device=%s",
                        exc.response.status_code,
                        self.config.device_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - transient heartbeat issue must not crash task execution
                    logger.debug("[aatm-worker] transient keepalive heartbeat error: %s", type(exc).__name__)

        work_task = asyncio.create_task(self._run_task_steps(task, trace))
        keepalive_enabled = self._client is not None or bool(self.config.token)
        keepalive_task = asyncio.create_task(_keepalive_loop()) if keepalive_enabled else None

        try:
            if keepalive_task is None:
                result = await work_task
            else:
                done, _ = await asyncio.wait(
                    {work_task, keepalive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if keepalive_task in done and keepalive_401 is not None:
                    if not work_task.done():
                        work_task.cancel()
                        try:
                            await work_task
                        except asyncio.CancelledError:
                            pass
                    total_ms = round((time.perf_counter() - exec_t0) * 1000, 2)
                    trace.append(
                        {
                            "step": "keepalive_heartbeat",
                            "action": "heartbeat",
                            "status": "revoked",
                            "exit_code": 401,
                            "elapsed_ms": total_ms,
                            "detail": "Halted mid-task: device token revoked (HTTP 401).",
                        }
                    )
                    return {
                        "success": False,
                        "summary": "Task aborted mid-execution: device token revoked (HTTP 401).",
                        "artifacts": [],
                        "error": self.stop_reason or "Device token revoked (HTTP 401).",
                        "trace": trace,
                        "total_elapsed_ms": total_ms,
                        "keepalive_heartbeats": keepalive_heartbeats,
                        "halted_on_revocation": True,
                    }
                result = await work_task
        finally:
            if keepalive_task is not None and not keepalive_task.done():
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass

        total_ms = round((time.perf_counter() - exec_t0) * 1000, 2)
        result.setdefault("trace", trace)
        result["total_elapsed_ms"] = total_ms
        result["keepalive_heartbeats"] = keepalive_heartbeats
        return result

    # ------------------------------------------------------------------ main loop
    async def run_once(self) -> dict[str, Any] | None:
        """One heartbeat/claim/execute/report cycle.

        Returns the executed task payload, or None when there was no work to do.
        """
        _ = self.workspace_root  # Do not heartbeat or claim work unless the device jail exists.
        await self.heartbeat()
        task = await self.claim()
        if not task:
            return None
        try:
            result = await self.execute_task(task)
        except Exception as exc:  # noqa: BLE001 - the daemon must never die on one task
            result = {
                "success": False,
                "summary": "Worker raised an exception",
                "artifacts": [],
                "error": str(exc),
                "trace": [],
                "total_elapsed_ms": 0.0,
                "keepalive_heartbeats": 0,
            }
        if self.token_revoked:
            if self._revocation_error is not None:
                raise self._revocation_error
            return None
        await self.report(
            task_id=task.get("id", ""),
            success=bool(result.get("success")),
            summary=str(result.get("summary", "")),
            artifacts=result.get("artifacts", []),
            error=result.get("error"),
            trace=result.get("trace", []),
            actual_deliverables=result.get("actual_deliverables", []),
            total_elapsed_ms=result.get("total_elapsed_ms"),
            keepalive_heartbeats=int(result.get("keepalive_heartbeats", 0)),
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
    parser.add_argument("--keepalive-seconds", type=float, default=env.keepalive_seconds)
    parser.add_argument("--max-concurrent", type=int, default=env.max_concurrent)
    parser.add_argument("--max-iterations", type=int, default=env.max_iterations)
    parser.add_argument("--command-timeout", type=int, default=env.command_timeout)
    parser.add_argument("--device-type", default=env.device_type)
    parser.add_argument("--sandbox-mode", default=env.sandbox_mode, choices=("process", "docker"))
    parser.add_argument("--sandbox-memory-mb", type=int, default=env.sandbox_memory_mb)
    parser.add_argument("--sandbox-cpu-seconds", type=int, default=env.sandbox_cpu_seconds)
    parser.add_argument("--sandbox-cpu-limit", type=float, default=env.sandbox_cpu_limit)
    parser.add_argument("--sandbox-pids-limit", type=int, default=env.sandbox_pids_limit)
    parser.add_argument("--sandbox-docker-image", default=env.sandbox_docker_image)
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
        keepalive_seconds=args.keepalive_seconds,
        max_concurrent=args.max_concurrent,
        max_iterations=args.max_iterations,
        command_timeout=args.command_timeout,
        device_type=args.device_type,
        sandbox_mode=args.sandbox_mode,
        sandbox_memory_mb=args.sandbox_memory_mb,
        sandbox_cpu_seconds=args.sandbox_cpu_seconds,
        sandbox_cpu_limit=args.sandbox_cpu_limit,
        sandbox_pids_limit=args.sandbox_pids_limit,
        sandbox_docker_image=args.sandbox_docker_image,
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
                "max_iterations": config.max_iterations,
                "command_timeout": config.command_timeout,
                "sandbox_mode": config.sandbox_mode,
                "sandbox_memory_mb": config.sandbox_memory_mb,
                "sandbox_cpu_limit": config.sandbox_cpu_limit,
                "sandbox_pids_limit": config.sandbox_pids_limit,
                "sandbox_docker_image": config.sandbox_docker_image,
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
