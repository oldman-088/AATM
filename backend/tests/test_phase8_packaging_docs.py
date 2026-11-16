"""Phase 8 verification tests: packaging, version parity, Alembic single head, scripts, and docs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_phase8_release_version_parity():
    settings = get_settings()
    assert settings.APP_VERSION == "3.2.0"
    pkg = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == "3.2.0"


async def test_phase8_health_reports_v3():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["version"] == "3.2.0"


def test_phase8_alembic_has_single_head():
    cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1


def test_phase8_ubuntu_and_windows_scripts_and_dockerfiles_present():
    ubuntu_scripts = [
        "install.sh",
        "start.sh",
        "stop.sh",
        "verify.sh",
        "start-worker.sh",
        "backup.sh",
        "restore.sh",
        "update.sh",
    ]
    for name in ubuntu_scripts:
        path = REPO_ROOT / "scripts" / "ubuntu" / name
        assert path.is_file(), f"Missing scripts/ubuntu/{name}"
        syntax = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert syntax.returncode == 0, f"Syntax error in {name}: {syntax.stderr}"

    windows_scripts = [
        "install.ps1",
        "start.ps1",
        "stop.ps1",
        "verify.ps1",
        "start-worker.ps1",
        "backup.ps1",
        "restore.ps1",
        "update.ps1",
    ]
    for name in windows_scripts:
        assert (REPO_ROOT / "scripts" / "windows" / name).is_file(), f"Missing scripts/windows/{name}"

    for rel in [
        "docker/sandbox.Dockerfile",
        "docker/worker.Dockerfile",
        "docker-compose.yml",
        ".github/workflows/ci.yml",
        "README.md",
        "docs/OPERATIONS.md",
        "CHANGELOG.md",
    ]:
        assert (REPO_ROOT / rel).is_file(), f"Missing {rel}"

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "3.2.0" in readme
    assert "DEFAULT_ADMIN_PASSWORD" in readme
    assert "aatm-admin-2026" not in readme
