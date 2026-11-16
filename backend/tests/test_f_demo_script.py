"""Item F.4 — scripts/demo_e2e.sh.

The full demo writes to a GitHub repository and needs a running Ollama model, so it is run by the
owner, not by CI. These tests cover everything that can be verified offline:
* argument validation and the side-effect-free ``--check`` mode;
* the exact API sequence the script drives (login + forced password change, Ollama provider via
  env: reference, agent provider assignment, project creation, Arabic directive, timeline/PR polling)
  is accepted by the real application with the payload shapes the script's jq filters expect.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import get_db
from app.main import app
from app.models import Task, User

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_e2e.sh"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                          env={**os.environ, **(env or {})})


def test_demo_script_validates_arguments_and_check_mode_has_no_side_effects(tmp_path):
    assert _run("--help").returncode == 0
    assert "choose --check" in _run().stderr
    assert "OWNER/NAME" in _run("--repo", "not-a-slug").stderr
    # Unreachable Ollama -> clear MISSING line and exit 2; nothing is created.
    res = _run("--check", env={"OLLAMA_URL": "http://127.0.0.1:9", "TMPDIR": str(tmp_path)})
    assert res.returncode == 2 and "Ollama server not reachable" in res.stdout
    assert list(tmp_path.iterdir()) == []


async def test_demo_api_sequence_is_accepted_by_the_application(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "DEFAULT_ADMIN_EMAIL", "demo-admin@aatm.local")
    monkeypatch.setattr(settings, "DEFAULT_ADMIN_PASSWORD", "first-demo-password-123")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama")
    async with factory() as db:
        await seed_initial_data(db)
        await db.execute(update(Task).values(status="completed"))
        await db.commit()
        admin = (await db.execute(select(User).where(User.email == "demo-admin@aatm.local"))).scalar_one()
        assert admin.must_change_password

    async def _db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            base = "/api/v1"
            tok = (await c.post(f"{base}/auth/login", json={"email": "demo-admin@aatm.local", "password": "first-demo-password-123"})).json()["access_token"]
            h = {"Authorization": f"Bearer {tok}"}
            r = await c.post(f"{base}/auth/change-password", headers=h, json={"current_password": "first-demo-password-123", "new_password": "second-demo-password-456"})
            assert r.status_code == 200, r.text
            tok = (await c.post(f"{base}/auth/login", json={"email": "demo-admin@aatm.local", "password": "second-demo-password-456"})).json()["access_token"]
            h = {"Authorization": f"Bearer {tok}"}
            r = await c.post(f"{base}/providers", headers=h, json={
                "id": "ollama-demo", "name": "Ollama (demo)", "provider_type": "openai_compatible",
                "endpoint_url": "http://127.0.0.1:11434/v1", "credential_ref": "env:OLLAMA_API_KEY",
                "default_model": "qwen2.5-coder:7b", "available_models": ["qwen2.5-coder:7b"], "priority": 1, "daily_limit_usd": 1})
            assert r.status_code == 201, r.text
            agents = (await c.get(f"{base}/agents", headers=h)).json()
            ids = [a["id"] for a in agents if a["role"] in ("manager", "worker", "reviewer")]
            assert "manager-0" in ids and len(ids) >= 5
            for agent_id in ids:
                r = await c.patch(f"{base}/agents/{agent_id}", headers=h, json={"provider_id": "ollama-demo", "model_name": "qwen2.5-coder:7b"})
                assert r.status_code == 200 and r.json()["provider_id"] == "ollama-demo", r.text
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            r = await c.post(f"{base}/projects", headers=h, json={"name": "AATM demo", "repo_path": str(repo), "repo_url": "https://github.com/acme/aatm-demo",
                                                                  "default_branch": "main", "worktree_mode": True, "auto_pr": True})
            assert r.status_code == 201, r.text
            pid = r.json()["id"]
            r = await c.post(f"{base}/chat", headers=h, json={"project_id": pid, "content": "أنشئ وحدة حاسبة صغيرة بلغة بايثون"})
            assert r.status_code == 201, r.text
            timeline = (await c.get(f"{base}/projects/{pid}/timeline", headers=h)).json()
            assert timeline["steps"][-1]["id"] == "completed" and "state" in timeline["steps"][-1]
            prs = (await c.get(f"{base}/reviews/pull-requests", params={"project_id": pid}, headers=h)).json()
            assert isinstance(prs, list)
            assert (await c.get(f"{base}/projects/{pid}/completion", headers=h)).status_code == 200
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()
