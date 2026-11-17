"""Tests must never run against the live database (data/aatm.db): many of them drop every table."""

from __future__ import annotations

from app.core.config import ROOT_DIR, get_settings
from app.db.session import engine

LIVE_DB = (ROOT_DIR / "data" / "aatm.db").resolve()


def test_tests_use_an_isolated_database_not_the_live_one():
    settings = get_settings()
    for url in (settings.DATABASE_URL, settings.get_sync_database_url(), str(engine.url)):
        assert str(LIVE_DB) not in url, url
        assert "data/aatm.db" not in url.replace("\\\\", "/"), url
