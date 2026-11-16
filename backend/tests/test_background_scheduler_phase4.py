from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.memory.engine import MemoryEngine
from app.models import BrowserProfile, MemoryEntry, Project
from app.qa.engine import qa_engine
from app.runtime.background_scheduler import BackgroundScheduler


@pytest.fixture
async def db_session(monkeypatch, tmp_path) -> AsyncSession:
    settings = get_settings()
    monkeypatch.setattr(settings, "BROWSER_PROFILES_DIR", tmp_path / "browser_profiles")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        yield db


def test_scheduler_interval_settings_are_positive_and_configurable():
    defaults = Settings(_env_file=None)
    assert defaults.SCHEDULER_INTERVAL_SECONDS == 5
    assert defaults.SELF_HEALING_INTERVAL_SECONDS == 60

    configured = Settings(
        _env_file=None,
        SCHEDULER_INTERVAL_SECONDS=0.25,
        SELF_HEALING_INTERVAL_SECONDS=90,
    )
    assert configured.SCHEDULER_INTERVAL_SECONDS == 0.25
    assert configured.SELF_HEALING_INTERVAL_SECONDS == 90

    with pytest.raises(ValueError):
        Settings(_env_file=None, SCHEDULER_INTERVAL_SECONDS=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, SELF_HEALING_INTERVAL_SECONDS=-1)


@pytest.mark.asyncio
async def test_orchestration_cycle_runs_autonomously_without_an_api_call():
    class RecordingScheduler:
        def __init__(self):
            self.calls = 0
            self.two_cycles_completed = asyncio.Event()

        async def run_orchestration_cycle(self, _db):
            self.calls += 1
            if self.calls >= 2:
                self.two_cycles_completed.set()
            return {"dispatched": []}

    class RecordingSelfHealer:
        calls = 0

        async def run_self_healing(self, _db):
            self.calls += 1
            return {}

    @asynccontextmanager
    async def session_factory():
        yield object()

    scheduler = RecordingScheduler()
    self_healer = RecordingSelfHealer()
    background = BackgroundScheduler(
        session_factory=session_factory,
        scheduler=scheduler,
        self_healer=self_healer,
        interval_seconds=0.01,
        self_healing_interval_seconds=60,
    )
    task = asyncio.create_task(background.run_orchestration_forever())
    try:
        await asyncio.wait_for(scheduler.two_cycles_completed.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert scheduler.calls >= 2
    assert self_healer.calls == 0


@pytest.mark.asyncio
async def test_self_healing_runs_on_its_configured_interval():
    class RecordingScheduler:
        async def run_orchestration_cycle(self, _db):
            return {}

    class RecordingSelfHealer:
        def __init__(self):
            self.calls = 0
            self.called = asyncio.Event()

        async def run_self_healing(self, _db):
            self.calls += 1
            self.called.set()
            return {}

    @asynccontextmanager
    async def session_factory():
        yield object()

    self_healer = RecordingSelfHealer()
    background = BackgroundScheduler(
        session_factory=session_factory,
        scheduler=RecordingScheduler(),
        self_healer=self_healer,
        interval_seconds=1,
        self_healing_interval_seconds=0.02,
    )
    task = asyncio.create_task(background.run_self_healing_forever())
    try:
        await asyncio.sleep(0)
        assert self_healer.calls == 0
        await asyncio.wait_for(self_healer.called.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert self_healer.calls == 1


@pytest.mark.asyncio
async def test_self_healing_only_recovers_profiles_marked_crashed(db_session, tmp_path, monkeypatch):
    from app.browser import manager as browser_manager_module
    from app.browser.manager import browser_manager

    healthy = BrowserProfile(
        id="phase4-healthy-profile",
        name="Healthy browser",
        agent_id="worker-1",
        profile_dir=str(tmp_path / "healthy-profile"),
        status="running",
        lock_owner="worker-1",
        crash_count=0,
    )
    crashed = BrowserProfile(
        id="phase4-crashed-profile",
        name="Crashed browser",
        agent_id="worker-2",
        profile_dir=str(tmp_path / "crashed-profile"),
        status="crashed",
        crash_count=2,
    )
    db_session.add_all([healthy, crashed])
    await db_session.commit()

    monkeypatch.setattr(
        browser_manager_module,
        "detect_playwright_capabilities",
        lambda: {
            "engine_mode": "not_implemented",
            "chromium_installed": False,
            "playwright_installed": False,
            "executable_path": None,
            "browser_source": None,
            "playwright_browsers_path": None,
        },
    )
    recovered_ids: list[str] = []
    actual_recovery = browser_manager.simulate_crash_and_recover

    async def record_recovery(db, profile, recover=True):
        recovered_ids.append(profile.id)
        return await actual_recovery(db, profile, recover=recover)

    monkeypatch.setattr(browser_manager, "simulate_crash_and_recover", record_recovery)

    result = await qa_engine.run_self_healing(db_session)

    await db_session.refresh(healthy)
    await db_session.refresh(crashed)
    assert recovered_ids == [crashed.id]
    assert result["browsers_recovered"] == []
    assert result["browser_recovery_failures"] == [
        {
            "profile_id": crashed.id,
            "status_code": 501,
            "reason": "Browser recovery is not implemented on this host because Playwright Chromium is unavailable.",
        }
    ]
    assert healthy.status == "running"
    assert healthy.crash_count == 0
    assert healthy.lock_owner == "worker-1"
    assert crashed.status == "crashed"
    assert crashed.crash_count == 3


@pytest.mark.asyncio
async def test_memory_search_filters_and_limits_candidates_in_sql(db_session):
    project = (
        await db_session.execute(select(Project).where(Project.slug == "aatm-core"))
    ).scalar_one()
    distractors = [
        MemoryEntry(
            scope="project",
            visibility="project",
            project_id=project.id,
            title=f"Nebula archive {index}",
            content="Background observation record.",
            tags=["astronomy"],
            importance=5,
        )
        for index in range(20)
    ]
    relevant = MemoryEntry(
        scope="project",
        visibility="project",
        project_id=project.id,
        title="Nebula pulsar flux target",
        content="The full query is represented in this relevant entry.",
        tags=["radio astronomy"],
        importance=8,
    )
    wrong_scope = MemoryEntry(
        scope="employee",
        visibility="project",
        project_id=project.id,
        title="Nebula pulsar flux wrong scope",
        content="Must be filtered before scoring.",
    )
    wrong_project = MemoryEntry(
        scope="project",
        visibility="project",
        project_id="different-project",
        title="Nebula pulsar flux wrong project",
        content="Must also be filtered before scoring.",
    )
    db_session.add_all([*distractors, relevant, wrong_scope, wrong_project])
    await db_session.commit()

    statements: list[tuple[str, tuple[object, ...]]] = []

    def capture_select(_connection, _cursor, statement, parameters, _context, _executemany):
        if "from memory_entries" in statement.lower():
            statements.append((statement, tuple(parameters)))

    event.listen(engine.sync_engine, "before_cursor_execute", capture_select)
    try:
        results = await MemoryEngine().search(
            db_session,
            query="nebula pulsar flux",
            scope="project",
            project_id=project.id,
            limit=2,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_select)

    assert results
    assert results[0]["id"] == relevant.id
    assert len(results) <= 2
    assert all(entry["id"] not in {wrong_scope.id, wrong_project.id} for entry in results)
    assert len(statements) == 1
    statement, parameters = statements[0]
    sql = statement.lower()
    assert " limit " in f" {sql} "
    assert parameters[-2:] == (10, 0)
    assert "memory_entries.scope =" in sql
    assert "memory_entries.project_id =" in sql
    assert " like " in sql
