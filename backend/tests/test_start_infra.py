"""scripts/ubuntu/start-infra.sh starts only Redis and RabbitMQ, and never blocks start.sh."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ubuntu" / "start-infra.sh"


def _run(tmp_path: Path, docker_exit: int | None, **env: str) -> tuple[subprocess.CompletedProcess, Path]:
    calls = tmp_path / "docker.calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if docker_exit is not None:
        fake = bin_dir / "docker"
        fake.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit {docker_exit}\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    path = str(bin_dir) if docker_exit is not None else f"{bin_dir}:/nonexistent"
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env={**os.environ, "PATH": f"{path}:/usr/bin:/bin" if docker_exit is not None else path, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls


def test_starts_redis_and_rabbitmq_but_not_postgres(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, 0)
    assert result.returncode == 0
    args = calls.read_text().split()
    assert args[:2] == ["compose", "--env-file"]
    assert "redis" in args and "rabbitmq" in args and "postgres" not in args
    assert "--no-recreate" in args


def test_compose_failure_is_a_warning(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, 1)
    assert result.returncode == 0
    assert "WARNING" in result.stderr


def test_can_be_disabled(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, 0, AATM_INFRA_AUTOSTART="false")
    assert result.returncode == 0
    assert not calls.exists()


def test_start_sh_calls_it() -> None:
    assert "scripts/ubuntu/start-infra.sh" in (ROOT / "scripts" / "ubuntu" / "start.sh").read_text()
