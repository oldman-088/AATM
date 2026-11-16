from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]

_PG_PREFIXES = (
    "postgres://",
    "postgresql://",
    "postgresql+asyncpg://",
    "postgresql+psycopg2://",
    "postgresql+psycopg://",
)


def normalize_async_database_url(url: str) -> str:
    """Normalize a database URL for SQLAlchemy's async engine (`create_async_engine`).

    Converts `postgres://`, `postgresql://`, `postgresql+asyncpg://`, and
    `postgresql+psycopg2://` into `postgresql+psycopg://` (using the installed
    `psycopg[binary]` v3 async-capable driver) and upgrades bare `sqlite:///`
    URLs to `sqlite+aiosqlite:///`.
    """
    raw = (url or "").strip()
    for prefix in _PG_PREFIXES:
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix) :]
    if raw.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + raw[len("sqlite:///") :]
    return raw


def normalize_sync_database_url(url: str) -> str:
    """Normalize a database URL for synchronous SQLAlchemy / Alembic operations.

    Converts `postgres://`, `postgresql://`, `postgresql+asyncpg://`, and
    `postgresql+psycopg2://` into `postgresql+psycopg://` and downgrades
    `sqlite+aiosqlite:///` to `sqlite:///`.
    """
    raw = (url or "").strip()
    for prefix in _PG_PREFIXES:
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix) :]
    if raw.startswith("sqlite+aiosqlite:///"):
        return "sqlite:///" + raw[len("sqlite+aiosqlite:///") :]
    return raw


def normalize_database_url(url: str, *, async_driver: bool = True) -> str:
    """Normalize a database URL for either async (`async_driver=True`) or sync engines."""
    if async_driver:
        return normalize_async_database_url(url)
    return normalize_sync_database_url(url)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    APP_NAME: str = "AATM — AI Agent Team Manager"
    APP_VERSION: str = "2.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    HOST: str = "127.0.0.1"
    PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173"

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
    ARENA_BRIDGE_TIMEOUT: float = 600.0
    ARENA_BRIDGE_MAX_CONCURRENCY: int = Field(default=1, ge=1)
    AGENT_LOOP_ENABLED: bool = True
    AGENT_LOOP_POLL_SECONDS: float = Field(default=3.0, gt=0)
    AGENT_MAX_ITERATIONS: int = Field(default=8, ge=1)
    AGENT_MAX_PARALLEL: int = Field(default=1, ge=1)
    AGENT_TASK_BUDGET_USD: float = Field(default=5.0, ge=0)
    SCHEDULER_INTERVAL_SECONDS: float = Field(default=5.0, gt=0)
    SELF_HEALING_INTERVAL_SECONDS: float = Field(default=60.0, gt=0)
    PROVIDER_MAX_RETRIES: int = Field(default=2, ge=0)
    PROVIDER_RETRY_AFTER_MAX_SECONDS: float = Field(default=30.0, ge=0)
    ANTHROPIC_API_KEY_REF: str = "env:ANTHROPIC_API_KEY"
    MONTHLY_BUDGET_USD: float = 100.0
    DAILY_BUDGET_USD: float = 20.0

    GITHUB_WEBHOOK_SECRET: str = Field(
        default="",
        validation_alias=AliasChoices("GITHUB_WEBHOOK_SECRET", "AATM_GITHUB_WEBHOOK_SECRET"),
    )
    AATM_SANDBOX_MODE: str = Field(
        default="docker",
        validation_alias=AliasChoices("AATM_SANDBOX_MODE", "SANDBOX_MODE"),
    )
    AATM_ALLOW_PROCESS_SANDBOX: bool = False
    SANDBOX_MEMORY_MB: int = 512
    SANDBOX_CPU_SECONDS: int = 30
    SANDBOX_MAX_PROCS: int = 64
    SANDBOX_DOCKER_IMAGE: str = "aatm-sandbox:latest"

    @model_validator(mode="after")
    def default_agent_parallel_from_bridge(self) -> "Settings":
        if "AGENT_MAX_PARALLEL" not in self.model_fields_set:
            self.AGENT_MAX_PARALLEL = max(1, self.ARENA_BRIDGE_MAX_CONCURRENCY)
        return self

    # Supports 20+ concurrent AI employees subject to host capacity
    MAX_CONCURRENT_WORKERS: int = 25
    MAX_CONCURRENT_BROWSERS: int = 10
    STALLED_TASK_MINUTES: int = 15

    def get_cors_origins(self) -> list[str]:
        origins = [o.strip() for o in (self.CORS_ORIGINS or "").split(",") if o.strip()]
        return origins or ["http://localhost:5173"]

    @property
    def AATM_GITHUB_WEBHOOK_SECRET(self) -> str:
        return self.GITHUB_WEBHOOK_SECRET

    @property
    def SANDBOX_MODE(self) -> str:
        return self.AATM_SANDBOX_MODE

    def get_async_database_url(self) -> str:
        return normalize_async_database_url(self.DATABASE_URL)

    def get_sync_database_url(self) -> str:
        if self.DATABASE_URL.startswith(_PG_PREFIXES):
            return normalize_sync_database_url(self.DATABASE_URL)
        return normalize_sync_database_url(self.SYNC_DATABASE_URL)


_PLACEHOLDER_SECRETS = frozenset(
    {
        "aatm-dev-secret-change-in-production-32bytes-minimum",
        "change-this-to-a-secure-random-32-plus-char-secret",
        "changeme",
        "secret",
    }
)
_PLACEHOLDER_PASSWORDS = frozenset(
    {
        "aatm-admin-2026",
        "change-me-immediately",
        "admin",
        "password",
    }
)


def validate_runtime_security(settings: Settings) -> None:
    """Fail closed at startup if `ENVIRONMENT=production` uses weak/default settings."""
    if (settings.ENVIRONMENT or "").strip().lower() != "production":
        return

    secret = (settings.SECRET_KEY or "").strip()
    if len(secret) < 32 or secret in _PLACEHOLDER_SECRETS or secret.lower().startswith("change-"):
        raise RuntimeError(
            "Refusing to start in production with a default, placeholder, or short (<32 chars) SECRET_KEY."
        )

    admin_pw = (settings.DEFAULT_ADMIN_PASSWORD or "").strip()
    if len(admin_pw) < 12 or admin_pw in _PLACEHOLDER_PASSWORDS or admin_pw.lower().startswith("change-"):
        raise RuntimeError(
            "Refusing to start in production with a default or weak (<12 chars) DEFAULT_ADMIN_PASSWORD."
        )

    sandbox_mode = (settings.AATM_SANDBOX_MODE or "docker").strip().lower()
    if sandbox_mode == "process" and not settings.AATM_ALLOW_PROCESS_SANDBOX:
        raise RuntimeError(
            "Refusing to run with AATM_SANDBOX_MODE=process in production unless AATM_ALLOW_PROCESS_SANDBOX=true is explicitly set."
        )


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.DATA_DIR.mkdir(parents=True, exist_ok=True)
    s.BROWSER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    s.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    s.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    return s
