"""Pytest configuration for AATM backend tests."""

import os
from pathlib import Path

# In-sandbox unit/E2E workspace execution tests run without a Docker daemon;
# security tests explicitly verify that `Settings().AATM_SANDBOX_MODE == "docker"`
# and that production mode refuses un-exempted `process` execution.
os.environ.setdefault("AATM_SANDBOX_MODE", "process")
# Existing tests were written against the demo data set; a fresh install (SEED_DEMO_DATA=false)
# is covered by test_e13_no_fake_data.py, which overrides this per test.
os.environ.setdefault("SEED_DEMO_DATA", "true")

# Never touch the live database. The app's default DATABASE_URL is an absolute path to the real
# data/aatm.db, and many tests drop and recreate every table, so without this a test run wiped the
# running AATM instance (profiles, token hashes, projects, users). Tests get a fresh temporary SQLite
# database instead; set AATM_TEST_DATABASE_URL + AATM_TEST_SYNC_DATABASE_URL to use a specific one.
import tempfile  # noqa: E402

if os.environ.get("AATM_TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["AATM_TEST_DATABASE_URL"]
    os.environ["SYNC_DATABASE_URL"] = os.environ.get("AATM_TEST_SYNC_DATABASE_URL", "")
else:
    _TEST_DB = Path(tempfile.mkdtemp(prefix="aatm-tests-")) / "aatm-test.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"
    os.environ["SYNC_DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

# Phase 6 tests run the real bridge (repo-root package `bridge`) next to the backend.
import sys  # noqa: E402

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)


def _create_test_schema() -> None:
    """Tests that do not reset the database themselves still find every table."""
    from sqlalchemy import create_engine

    import app.models  # noqa: F401 - registers every table on Base.metadata
    from app.db.base import Base

    if not os.environ.get("SYNC_DATABASE_URL"):
        return
    engine = create_engine(os.environ["SYNC_DATABASE_URL"])
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


_create_test_schema()
