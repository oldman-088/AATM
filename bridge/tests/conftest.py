"""Pytest configuration for the bridge tests (they import the backend app)."""

import os
import sys
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parents[2] / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

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


def _create_test_schema() -> None:
    """Tests that do not reset the database themselves still find every table."""
    import app.models  # noqa: F401 - registers every table on Base.metadata
    from app.db.base import Base
    from sqlalchemy import create_engine

    if not os.environ.get("SYNC_DATABASE_URL"):
        return
    engine = create_engine(os.environ["SYNC_DATABASE_URL"])
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


_create_test_schema()
