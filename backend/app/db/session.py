from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import (
    get_settings,
    normalize_async_database_url,
    normalize_database_url,
    normalize_sync_database_url,
)


def _make_engine(database_url: str | None = None) -> AsyncEngine:
    settings = get_settings()
    raw_url = database_url if database_url is not None else settings.DATABASE_URL
    url = normalize_async_database_url(raw_url)
    if url.startswith("sqlite"):
        return create_async_engine(
            url,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False},
        )
    return create_async_engine(
        url,
        echo=False,
        future=True,
        pool_pre_ping=True,
    )


engine = _make_engine()
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


__all__ = [
    "AsyncSessionLocal",
    "_make_engine",
    "engine",
    "get_db",
    "normalize_async_database_url",
    "normalize_database_url",
    "normalize_sync_database_url",
]
