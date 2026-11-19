"""verify.sh --install must make a clean copy verifiable, frontend included (stubbed tools, no network)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def _clean_copy(tmp_path: Path, *, lock: bool) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "copy"
    (root / "scripts" / "ubuntu").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "ubuntu" / "verify.sh", root / "scripts" / "ubuntu" / "verify.sh")
    (root / "backend").mkdir()
    (root / "frontend").mkdir()
    (root / "frontend" / "package.json").write_text("{}")
    if lock:
        (root / "frontend" / "package-lock.json").write_text("{}")
    venv = root / ".venv" / "bin"
    venv.mkdir(parents=True)
    _stub(venv / "alembic", 'if [[ "$1" == heads ]]; then echo "abc (head)"; fi')
    _stub(venv / "ruff", "exit 0")
    _stub(venv / "python", "exit 0")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "npm.log"
    _stub(bin_dir / "npm", f'echo "$*" >> "{log}"; if [[ "$1" == ci || "$1" == install ]]; then mkdir -p node_modules; fi')
    _stub(bin_dir / "node", "echo v22.0.0")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return root, env


def _verify(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "scripts/ubuntu/verify.sh", *args], cwd=root, env=env, capture_output=True, text=True,
                          timeout=60)


@pytest.mark.parametrize(("lock", "command"), [(True, "ci --no-audit --no-fund"), (False, "install --no-audit --no-fund")])
def test_install_flag_installs_frontend_packages_on_a_clean_copy(tmp_path, lock, command):
    root, env = _clean_copy(tmp_path, lock=lock)
    result = _verify(root, env, "--install")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "frontend dependencies installed" in result.stdout
    calls = (tmp_path / "npm.log").read_text().splitlines()
    assert calls[0] == command
    assert "run --silent build" in calls


def test_without_install_a_missing_node_modules_still_fails(tmp_path):
    root, env = _clean_copy(tmp_path, lock=True)
    result = _verify(root, env)
    assert result.returncode != 0
    assert "frontend/node_modules missing" in result.stdout
    assert not (tmp_path / "npm.log").exists()
