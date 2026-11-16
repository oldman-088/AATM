"""Timezone normalisation helpers.

SQLite has no native timezone-aware datetime type: SQLAlchemy stores
`DateTime(timezone=True)` columns as naive UTC strings and reads them back without a
tzinfo. Comparing such a value with an aware `datetime` raises
`TypeError: can't compare offset-naive and offset-aware datetimes`, and comparing it in
SQL relies on string ordering of ISO timestamps, which is only accidentally correct.

Every timestamp comparison in AATM therefore normalises both sides to naive UTC first.
"""

from datetime import datetime, timezone


def to_naive_utc(value: datetime | None) -> datetime | None:
    """Return `value` as a naive UTC datetime (the representation SQLite stores)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def naive_utc_now() -> datetime:
    """Current UTC time as a naive datetime, matching the SQLite representation."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
