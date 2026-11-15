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
    APP_VERSION: str = "2.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    HOST: str = "0.0.0.0"
    PORT: int = 8000

    DATABASE_URL: str = f"sqlite+aiosqlite:///{ROOT_DIR / 'data' / 'aatm.db'}"
    SYNC_DATABASE_URL: str = f"sqlite:///{ROOT_DIR / 'data' / 'aatm.db'}"

    SECRET_KEY: str = "aatm-dev-secret-change-in-production-32bytes-minimum"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    DEFAULT_ADMIN_EMAIL: str = "admin@aatm.local"
    DEFAULT_ADMIN_PASSWORD: str = "aatm-admin-2026"

    DATA_DIR: Path = ROOT_DIR / "data"
    BROWSER_PROFILES_DIR: Path = ROOT_DIR / "data" / "browser_profiles"
    WORKSPACES_DIR: Path = ROOT_DIR / "data" / "workspaces"
    BACKUPS_DIR: Path = ROOT_DIR / "backups"
    REPO_ROOT: Path = ROOT_DIR

    ARENA_BRIDGE_URL: str = "http://127.0.0.1:4100"
    ANTHROPIC_API_KEY_REF: str = "env:ANTHROPIC_API_KEY"
    MONTHLY_BUDGET_USD: float = 100.0
    DAILY_BUDGET_USD: float = 20.0

    # Supports 20+ concurrent AI employees subject to host capacity
    MAX_CONCURRENT_WORKERS: int = 25
    MAX_CONCURRENT_BROWSERS: int = 10
    STALLED_TASK_MINUTES: int = 15


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.DATA_DIR.mkdir(parents=True, exist_ok=True)
    s.BROWSER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    s.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    s.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    return s
