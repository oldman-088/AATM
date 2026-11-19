"""Regression checks for the verification entry point."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_verify_documents_parallel_and_changed_modes():
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/ubuntu/verify.sh"), "--help"],
        capture_output=True, text=True, check=True,
    )
    assert "--no-parallel" in result.stdout
    assert "--changed" in result.stdout


def test_scripts_pass_ruff():
    subprocess.run(
        [str(ROOT / ".venv/bin/ruff"), "check", "--config", "backend/pyproject.toml", "scripts"],
        cwd=ROOT, check=True,
    )
