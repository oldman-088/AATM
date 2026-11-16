"""Phase 4 verification tests: worker sandbox isolation, env scrubbing, resource limits, and fail-closed Docker mode."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import User
from app.runtime.executor import AgentRuntimeExecutor
from app.runtime.sandbox import (
    SandboxExecutionPolicy,
    WorkspaceSandboxError,
    build_isolated_command,
    build_sanitized_env,
    resolve_contained_path,
)


@pytest.fixture()
async def phase4_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "phase4.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "WORKSPACES_DIR", tmp_path / "workspaces")
    async with session_factory() as db:
        await seed_initial_data(db)
    yield session_factory
    await engine.dispose()


def test_phase4_docker_command_enforces_nonroot_readonly_network_none_and_pull_never(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    cmd = build_isolated_command(
        ["pytest", "-q", "test_ok.py"],
        ws,
        sandbox_mode="docker",
        memory_mb=256,
        cpu_limit=0.5,
        pids_limit=32,
        docker_bin="/usr/bin/docker",
    )
    assert cmd[:6] == ["/usr/bin/docker", "run", "--rm", "--pull=never", "--network=none", "--read-only"]
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    assert "--user" in cmd and "10001:10001" in cmd
    assert "--pids-limit=32" in cmd
    assert "--memory=256m" in cmd
    assert "--cpus=0.5" in cmd
    assert f"{ws.resolve()}:/workspace:rw" in cmd
    assert "aatm-sandbox:latest" in cmd
    assert cmd[-3:] == ["pytest", "-q", "test_ok.py"]


def test_phase4_process_mode_blocked_in_production_without_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("AATM_ALLOW_PROCESS_SANDBOX", raising=False)
    monkeypatch.setattr(get_settings(), "AATM_ALLOW_PROCESS_SANDBOX", False)

    with pytest.raises(WorkspaceSandboxError, match="forbidden in production"):
        build_isolated_command(["python3", "-V"], ws, sandbox_mode="process")

    monkeypatch.setenv("AATM_ALLOW_PROCESS_SANDBOX", "true")
    allowed = build_isolated_command(["python3", "-V"], ws, sandbox_mode="process")
    assert allowed == ["python3", "-V"]


def test_phase4_env_scrubbing_and_path_traversal_blocking(tmp_path: Path):
    base_env = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "SECRET_KEY": "do-not-leak-secret",
        "ANTHROPIC_API_KEY": "sk-ant-do-not-leak",
        "ARENA_BRIDGE_TOKEN": "bridge-do-not-leak",
        "AATM_GITHUB_WEBHOOK_SECRET": "whsec-do-not-leak",
        "DATABASE_URL": "postgresql://" + "user:dummy" + "@host/db",
    }
    env = build_sanitized_env(
        extra={
            "HOME": "/tmp/ws",
            "CUSTOM_API_TOKEN": "should-be-stripped",
            "MY_PRIVATE_KEY": "should-be-stripped",
        },
        base_env=base_env,
    )
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/tmp/ws"
    for forbidden in (
        "SECRET_KEY",
        "ANTHROPIC_API_KEY",
        "ARENA_BRIDGE_TOKEN",
        "AATM_GITHUB_WEBHOOK_SECRET",
        "DATABASE_URL",
        "CUSTOM_API_TOKEN",
        "MY_PRIVATE_KEY",
    ):
        assert forbidden not in env

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = ws / "escape_link.txt"
    link.symlink_to(outside)

    with pytest.raises(WorkspaceSandboxError):
        resolve_contained_path(ws, "../../etc/passwd")
    with pytest.raises(WorkspaceSandboxError):
        resolve_contained_path(ws, "escape_link.txt")


async def test_phase4_async_executor_timeout_and_sandbox_status_endpoint(
    phase4_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    executor = AgentRuntimeExecutor()
    async with phase4_db() as db:
        res = await executor.run_workspace_command(
            db,
            "worker-1",
            ["python3", "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'CLEAN'))"],
            sandbox_mode="process",
        )
        assert res["exit_code"] == 0
        assert "CLEAN" in res["stdout"]

        timed_out = await executor.run_workspace_command(
            db,
            "worker-1",
            ["python3", "-c", "import time; time.sleep(5)"],
            policy=SandboxExecutionPolicy(sandbox_mode="process", timeout_seconds=1),
            sandbox_mode="process",
        )
        assert timed_out["exit_code"] == 124
        assert "timed out" in timed_out["stderr"].lower()

        with pytest.raises(WorkspaceSandboxError):
            await executor.run_workspace_command(db, "worker-1", ["bash", "-c", "id"], sandbox_mode="process")

    async def _override_db():
        async with phase4_db() as db:
            yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        async with phase4_db() as db:
            admin = (await db.execute(select(User).where(User.role == "admin"))).scalar_one()
            admin.must_change_password = False
            await db.commit()
            token = create_access_token(subject=admin.email, role=admin.role, token_version=admin.token_version)

        monkeypatch.setenv("AATM_SANDBOX_MODE", "docker")
        monkeypatch.setattr(shutil, "which", lambda _name: None)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/sandbox/status",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "SANDBOX_UNAVAILABLE"
            assert data["available"] is False
            assert data["policy"]["memory_mb"] >= 64
            assert "pytest" in data["policy"]["allowed_binaries"]
    finally:
        app.dependency_overrides.pop(get_db, None)
