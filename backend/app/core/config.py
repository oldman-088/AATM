from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "AATM — AI Agent Team Manager"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Supports both PostgreSQL (postgresql+psycopg://user:pass@host:5432/aatm)
    # and SQLite (sqlite+aiosqlite:///./data/aatm.db)
    DATABASE_URL: str = f"sqlite+aiosqlite:///{ROOT_DIR / 'data' / 'aatm.db'}"
    SYNC_DATABASE_URL: str = f"sqlite:///{ROOT_DIR / 'data' / 'aatm.db'}"

    SECRET_KEY: str = "aatm-dev-secret-change-in-production-32bytes-minimum"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    # Default Admin Bootstrap
    DEFAULT_ADMIN_EMAIL: str = "admin@aatm.local"
    DEFAULT_ADMIN_PASSWORD: str = "aatm-admin-2026"

    # Data & Profiles
    DATA_DIR: Path = ROOT_DIR / "data"
    BROWSER_PROFILES_DIR: Path = ROOT_DIR / "data" / "browser_profiles"
    BACKUPS_DIR: Path = ROOT_DIR / "backups"
    REPO_ROOT: Path = ROOT_DIR

    # AI Provider Defaults
    ARENA_BRIDGE_URL: str = "http://127.0.0.1:4100"
    ANTHROPIC_API_KEY_REF: str = "env:ANTHROPIC_API_KEY"
    MONTHLY_BUDGET_USD: float = 100.0
    DAILY_BUDGET_USD: float = 20.0

    # Concurrency & Orchestration
    MAX_CONCURRENT_WORKERS: int = 4
    MAX_CONCURRENT_BROWSERS: int = 2
    STALLED_TASK_MINUTES: int = 15


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.DATA_DIR.mkdir(parents=True, exist_ok=True)
    s.BROWSER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    s.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    return s
