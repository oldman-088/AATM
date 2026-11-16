"""Item F.1 — memory search: Postgres full-text search (GIN index) with the SQLite fallback.

The Postgres test runs only with AATM_TEST_POSTGRES_URL (a throwaway localhost container), e.g.
    docker run -d --rm --name aatm-pg-test -e POSTGRES_PASSWORD=<password> -p 127.0.0.1:57432:5432 postgres:16-alpine
    AATM_TEST_POSTGRES_URL=postgresql://postgres:<password>@127.0.0.1:57432/postgres pytest tests/test_f_memory.py
It creates and drops its own database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import normalize_async_database_url
from app.db.base import Base
from app.memory.engine import MemoryEngine
from app.models import MemoryEntry, Project

POSTGRES_URL = os.environ.get("AATM_TEST_POSTGRES_URL")
BACKEND_DIR = Path(__file__).resolve().parents[1]


def _entries(project_id: str, other_project_id: str) -> list[MemoryEntry]:
    return [
        MemoryEntry(scope="project", visibility="team", project_id=project_id, title="Checkout API design",
                    content="Use idempotency keys for payments.", tags=["payments", "api"], importance=5, created_by="manager-0"),
        MemoryEntry(scope="project", visibility="team", project_id=project_id, title="قرار التصميم",
                    content="استخدم قاعدة بيانات بوستجرس للمتجر", tags=["قاعدة"], importance=3, created_by="manager-0"),
        MemoryEntry(scope="employee", visibility="private", agent_id="worker-1", project_id=project_id, title="Private payments note",
                    content="worker-1 only: payments sandbox quirks", tags=[], importance=9, created_by="worker-1"),
        MemoryEntry(scope="organization", visibility="team", title="Org coding standard",
                    content="All payments code needs tests.", tags=["standards"], importance=1, created_by="admin"),
        MemoryEntry(scope="project", visibility="team", project_id=other_project_id, title="Other project payments",
                    content="Unrelated payments note", tags=[], importance=10, created_by="manager-0"),
    ]


async def _exercise(factory) -> str:
    async with factory() as db:
        project = Project(name="Shop", slug=f"shop-{uuid.uuid4().hex[:6]}", default_branch="main")
        other = Project(name="Other", slug=f"other-{uuid.uuid4().hex[:6]}", default_branch="main")
        db.add_all([project, other])
        await db.flush()
        db.add_all(_entries(project.id, other.id))
        await db.commit()
        engine = MemoryEngine()
        backend = engine.search_backend(db)["backend"]

        titles = [r["title"] for r in await engine.search(db, "payments", project_id=project.id, requesting_agent_id="worker-2")]
        assert "Checkout API design" in titles and "Org coding standard" in titles
        assert "Private payments note" not in titles, "private memory hidden from other employees"
        assert "Other project payments" not in titles, "project scoping kept"
        own = [r["title"] for r in await engine.search(db, "payments", project_id=project.id, requesting_agent_id="worker-1")]
        assert "Private payments note" in own
        assert "Private payments note" in [r["title"] for r in await engine.search(db, "payments", project_id=project.id, requesting_agent_id="manager-0")]

        arabic = await engine.search(db, "بوستجرس", project_id=project.id)
        assert [r["title"] for r in arabic] == ["قرار التصميم"]
        assert await engine.search(db, "nonexistentword", project_id=project.id) == []
        # Prefix/partial word match and ranking (title+content+tag hits beat a single hit).
        ranked = await engine.search(db, "idempot payments api", project_id=project.id, requesting_agent_id="worker-2")
        assert ranked[0]["title"] == "Checkout API design"
        # Operator-looking input is tokenized, never passed through as tsquery syntax.
        assert await engine.search(db, "payments & !(api) | ':*", project_id=project.id, requesting_agent_id="worker-2")
        assert len(await engine.search(db, "", project_id=project.id, limit=2)) == 2
        return backend


def test_engine_expression_matches_the_migration_index_expression():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mig", BACKEND_DIR / "alembic/versions/b7c8d9e0f1a2_memory_full_text_search_index.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    from app.memory.engine import FTS_DOCUMENT_SQL

    assert FTS_DOCUMENT_SQL.replace("memory_entries.", "") == mig.EXPRESSION


async def test_sqlite_fallback_keeps_scope_permissions_and_arabic(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mem.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        assert await _exercise(async_sessionmaker(engine, expire_on_commit=False)) == "token_overlap"
    finally:
        await engine.dispose()


@pytest.mark.skipif(not POSTGRES_URL, reason="AATM_TEST_POSTGRES_URL not set (Postgres FTS integration)")
async def test_postgres_full_text_search_uses_the_gin_index_from_the_migration():
    admin_url = normalize_async_database_url(POSTGRES_URL)
    name = f"aatm_fts_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    db_url = admin_url.rsplit("/", 1)[0] + f"/{name}"
    engine = create_async_engine(db_url)
    try:
        # The real migration chain, so the index under test is the one production gets.
        env = {**os.environ, "DATABASE_URL": db_url}
        proc = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND_DIR, env=env,
                              capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, proc.stderr[-2000:]
        factory = async_sessionmaker(engine, expire_on_commit=False)
        assert await _exercise(factory) == "postgres_fts"
        async with engine.connect() as conn:
            indexes = (await conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'memory_entries'"))).scalars().all()
            assert "ix_memory_entries_fts" in indexes
        # The query expression must match the index expression, or Postgres cannot use the index.
        async with factory() as db:
            await db.execute(text("SET enable_seqscan = off"))
            from app.memory import engine as mem

            stmt = mem.select(MemoryEntry).where(mem._fts_document().op("@@")(mem.func.to_tsquery(mem.SIMPLE_CONFIG, "payments:*")))
            compiled = stmt.compile(engine.sync_engine, compile_kwargs={"literal_binds": True})
            plan = "\n".join((await db.execute(text(f"EXPLAIN {compiled}"))).scalars().all())
            assert "ix_memory_entries_fts" in plan, plan
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
