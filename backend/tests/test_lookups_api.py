"""Tests for the phone-number lookup API: E.164 normalization, job creation,
status/result retrieval, provider-result ingestion with aggregation, and the
Alembic migration that creates the lookup tables."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.api.routes.lookups import aggregate_provider_results
from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import ProviderResult
from app.schemas.phone import PHONE_INVALID_ERROR, normalize_phone_e164

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG_PATH = BACKEND_ROOT / "alembic.ini"
PRE_LOOKUP_REVISION = "e0f1a2b3c4d5"


@pytest.fixture(scope="module")
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="module")
async def auth_headers(client: AsyncClient):
    settings = get_settings()
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    data = resp.json()
    token = data["access_token"]
    if data["user"].get("must_change_password"):
        rot = await client.post(
            "/api/v1/auth/change-password",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "current_password": settings.DEFAULT_ADMIN_PASSWORD,
                "new_password": "TestSuiteRotatedPassword!2026",
            },
        )
        assert rot.status_code == 200
        token = rot.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# E.164 normalization (unit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+14155552671", "+14155552671"),
        ("+1 (415) 555-2671", "+14155552671"),
        ("0040 721 234 567", "+40721234567"),
        ("0040-721-234-567", "+40721234567"),
        ("  +40 721 234 567  ", "+40721234567"),
        ("+40.721.234.567", "+40721234567"),
        ("٠٠٤٠ ٧٢١ ٢٣٤ ٥٦٧", "+40721234567"),  # Arabic-Indic digits, 00 prefix
        ("+٤٠٧٢١٢٣٤٥٦٧", "+40721234567"),  # Arabic-Indic digits after +
        ("۰۰۴۰۷۲۱۲۳۴۵۶۷", "+40721234567"),  # Persian digits, 00 prefix
        ("+123456789012345", "+123456789012345"),  # 15 digits is the E.164 maximum
    ],
)
def test_normalize_phone_e164_accepts_and_canonicalizes(raw: str, expected: str) -> None:
    assert normalize_phone_e164(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not-a-phone",
        "0721234567",  # national number without an international prefix
        "40721234567",  # national number without an international prefix
        "+0123456789",  # leading zero after +
        "00",
        "+",
        "++40721234567",
        "+1234567890123456",  # 16 digits exceeds the E.164 maximum
        "+40 721 234 567 ext 9",
        "tel:+40721234567",
    ],
)
def test_normalize_phone_e164_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError, match="E.164"):
        normalize_phone_e164(raw)


def test_normalize_phone_e164_rejects_non_string_values() -> None:
    with pytest.raises(ValueError) as exc_info:
        normalize_phone_e164(12345)  # type: ignore[arg-type]
    assert str(exc_info.value) == PHONE_INVALID_ERROR


# ---------------------------------------------------------------------------
# Job creation + retrieval (API)
# ---------------------------------------------------------------------------


async def test_create_lookup_requires_authentication(client: AsyncClient):
    resp = await client.post("/api/v1/lookups", json={"phone": "+14155552671"})
    assert resp.status_code == 401
    resp = await client.get("/api/v1/lookups/some-job")
    assert resp.status_code == 401


async def test_create_lookup_returns_job_id_and_queued_status(client: AsyncClient, auth_headers):
    resp = await client.post(
        "/api/v1/lookups",
        headers=auth_headers,
        json={"phone": "0040 721 234 567"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["job_id"]
    assert data["status"] == "queued"
    assert data["phone_e164"] == "+40721234567"
    assert data["phone_raw"] == "0040 721 234 567"

    detail = await client.get(f"/api/v1/lookups/{data['job_id']}", headers=auth_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["job_id"] == data["job_id"]
    assert body["status"] == "queued"
    assert body["phone_e164"] == "+40721234567"
    assert body["result"] is None
    assert body["provider_results"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"phone": "abc"},
        {"phone": "0721 234 567"},
        {"phone": "+0123"},
        {"phone": "+1234567890123456"},
        {"phone": ""},
        {"phone": 12345},
        {},
    ],
)
async def test_create_lookup_rejects_invalid_phone_with_422(client: AsyncClient, auth_headers, payload):
    resp = await client.post("/api/v1/lookups", headers=auth_headers, json=payload)
    assert resp.status_code == 422


async def test_get_unknown_lookup_returns_404(client: AsyncClient, auth_headers):
    resp = await client.get("/api/v1/lookups/00000000-0000-0000-0000-000000000000", headers=auth_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Provider-result ingestion and aggregation (API)
# ---------------------------------------------------------------------------


async def test_provider_results_are_aggregated_and_finalized(client: AsyncClient, auth_headers):
    created = await client.post(
        "/api/v1/lookups",
        headers=auth_headers,
        json={"phone": "+1 (415) 555-2671"},
    )
    assert created.status_code == 201
    job_id = created.json()["job_id"]

    first = await client.post(
        f"/api/v1/lookups/{job_id}/results",
        headers=auth_headers,
        json={
            "provider": "numverify",
            "status": "success",
            "http_status": 200,
            "latency_ms": 120.5,
            "payload": {"carrier": "Vodafone", "country": "RO", "line_type": "mobile", "spam_score": 12, "extra": "keep-me"},
        },
    )
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "running"
    assert body["started_at"] is not None
    assert body["result"]["carrier"] == "Vodafone"
    assert body["result"]["summary"] == "Vodafone · RO · mobile"
    assert body["result"]["attributes"] == {"numverify": {"extra": "keep-me"}}
    assert len(body["provider_results"]) == 1

    second = await client.post(
        f"/api/v1/lookups/{job_id}/results",
        headers=auth_headers,
        json={
            "provider": "spam-check",
            "status": "success",
            "payload": {"name": "Ion Popescu", "spam_score": 87.5, "is_spam": True},
            "finalize": True,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "completed"
    assert body["completed_at"] is not None
    assert body["error_message"] is None
    result = body["result"]
    assert result["display_name"] == "Ion Popescu"  # reported by spam-check
    assert result["carrier"] == "Vodafone"  # reported by numverify
    assert result["country"] == "RO"
    assert result["line_type"] == "mobile"
    assert result["spam_score"] == 87.5  # maximum of the reported scores
    assert result["is_spam"] is True
    assert result["summary"] == "Ion Popescu · Vodafone · RO · mobile"
    assert {row["provider"] for row in body["provider_results"]} == {"numverify", "spam-check"}


async def test_provider_result_upsert_is_idempotent_per_provider(client: AsyncClient, auth_headers):
    created = await client.post("/api/v1/lookups", headers=auth_headers, json={"phone": "+33 6 12 34 56 78"})
    job_id = created.json()["job_id"]

    for attempt in range(2):
        resp = await client.post(
            f"/api/v1/lookups/{job_id}/results",
            headers=auth_headers,
            json={"provider": "single-provider", "status": "success", "payload": {"carrier": f"attempt-{attempt}"}},
        )
        assert resp.status_code == 200

    body = (await client.get(f"/api/v1/lookups/{job_id}", headers=auth_headers)).json()
    assert len(body["provider_results"]) == 1
    assert body["provider_results"][0]["payload"]["carrier"] == "attempt-1"
    assert body["result"]["carrier"] == "attempt-1"


async def test_finalize_without_any_success_marks_job_failed(client: AsyncClient, auth_headers):
    created = await client.post("/api/v1/lookups", headers=auth_headers, json={"phone": "+49 151 23456789"})
    job_id = created.json()["job_id"]

    resp = await client.post(
        f"/api/v1/lookups/{job_id}/results",
        headers=auth_headers,
        json={"provider": "broken-provider", "status": "timeout", "error_message": "upstream timed out", "finalize": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["completed_at"] is not None
    assert body["error_message"] == "every provider result failed or timed out"
    assert body["result"] is not None  # the aggregate row exists but carries no data
    assert body["result"]["carrier"] is None


async def test_submit_provider_result_unknown_job_returns_404(client: AsyncClient, auth_headers):
    resp = await client.post(
        "/api/v1/lookups/00000000-0000-0000-0000-000000000000/results",
        headers=auth_headers,
        json={"provider": "any", "status": "success"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Aggregation policy (unit)
# ---------------------------------------------------------------------------


def _provider_row(provider: str, status: str, payload: dict, minute: int) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=status,
        payload=payload,
        created_at=datetime(2026, 11, 19, 12, minute, tzinfo=timezone.utc),
    )


def test_aggregate_provider_results_merges_fields_with_documented_policy() -> None:
    rows = [
        _provider_row("later-provider", "success", {"carrier": "Should Not Win", "spam_score": 5}, minute=2),
        _provider_row("first-provider", "success", {"carrier": "Orange", "spam_score": 40}, minute=1),
        _provider_row("failed-provider", "error", {"carrier": "Ignored"}, minute=0),
        _provider_row("spam-provider", "success", {"is_spam": True, "spam_score": 90}, minute=3),
    ]
    aggregate = aggregate_provider_results(rows)

    # Only successful rows contribute; the earliest success wins conflicting fields.
    assert aggregate["carrier"] == "Orange"
    assert aggregate["spam_score"] == 90.0
    assert aggregate["is_spam"] is True
    assert "Ignored" not in str(aggregate)
    assert aggregate["summary"] == "Orange"
    assert set(aggregate["attributes"]) == {"first-provider", "later-provider", "spam-provider"}


def test_aggregate_provider_results_without_success_rows_is_empty() -> None:
    aggregate = aggregate_provider_results([_provider_row("p", "timeout", {"carrier": "X"}, minute=0)])
    assert aggregate["display_name"] is None
    assert aggregate["carrier"] is None
    assert aggregate["spam_score"] is None
    assert aggregate["is_spam"] is False
    assert aggregate["summary"] == ""
    assert aggregate["attributes"] == {}


# ---------------------------------------------------------------------------
# Alembic migration
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return config


def _point_settings_at(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()


def _table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _index_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_lookup_tables_migration_up_down_up(tmp_path, monkeypatch):
    database_path = tmp_path / "lookups-migration.sqlite"
    _point_settings_at(database_path, monkeypatch)
    config = _alembic_config()
    lookup_tables = {"lookup_jobs", "lookup_results", "provider_results"}
    try:
        command.upgrade(config, "head")
        tables = _table_names(database_path)
        assert lookup_tables <= tables
        indexes = _index_names(database_path)
        assert {
            "ix_lookup_jobs_phone_e164",
            "ix_lookup_jobs_status",
            "ix_lookup_jobs_created_at",
            "ix_lookup_results_job_id",
            "ix_provider_results_job_id",
            "ix_provider_results_provider",
        } <= indexes

        command.downgrade(config, PRE_LOOKUP_REVISION)
        assert lookup_tables.isdisjoint(_table_names(database_path))

        command.upgrade(config, "head")
        assert lookup_tables <= _table_names(database_path)
    finally:
        get_settings.cache_clear()


def test_lookup_migration_is_idempotent_on_create_all_databases(tmp_path, monkeypatch):
    """Dev databases built by ``Base.metadata.create_all`` (the test suite) already
    have the lookup tables; upgrading over them must not fail."""
    database_path = tmp_path / "lookups-create-all.sqlite"
    _point_settings_at(database_path, monkeypatch)
    config = _alembic_config()
    try:
        # Bring the schema to the pre-lookup head through migrations...
        command.upgrade(config, PRE_LOOKUP_REVISION)
        # ...then simulate the dev drift: create_all adds the missing lookup
        # tables exactly the way the test-suite bootstraps its schema.
        bootstrap = create_engine(f"sqlite:///{database_path.as_posix()}", poolclass=NullPool)
        try:
            Base.metadata.create_all(bootstrap)
        finally:
            bootstrap.dispose()
        assert {"lookup_jobs", "lookup_results", "provider_results"} <= _table_names(database_path)

        command.upgrade(config, "head")  # must be a no-op for the lookup tables
        assert {"lookup_jobs", "lookup_results", "provider_results"} <= _table_names(database_path)
    finally:
        get_settings.cache_clear()
