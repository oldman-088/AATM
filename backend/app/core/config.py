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


def resolve_sqlite_url_path(url: str) -> str:
    """Make the file path inside a SQLite URL absolute and create its directory.

    A relative SQLite URL such as ``sqlite+aiosqlite:///./data/aatm.db`` is resolved by SQLite
    against the *current working directory*. The backend is started from the repository root but
    Alembic runs from ``backend/``, so the same ``.env`` produced ``backend/data/aatm.db`` (missing
    directory → "unable to open database file"). Resolving relative paths against the repository
    root, and creating ``data/`` up front, makes migrations and the API agree on one file.
    """
    raw = (url or "").strip()
    if not raw.startswith("sqlite"):
        return url
    scheme, separator, rest = raw.partition(":///")
    if not separator:
        return url
    if not rest or rest == ":memory:" or rest.startswith("file:") or rest.startswith(":memory:?"):
        return url
    path = Path(rest)
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    else:
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"{scheme}:///{path.as_posix()}"


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
        return resolve_sqlite_url_path("sqlite+aiosqlite:///" + raw[len("sqlite:///") :])
    return resolve_sqlite_url_path(raw)


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
        return resolve_sqlite_url_path("sqlite:///" + raw[len("sqlite+aiosqlite:///") :])
    return resolve_sqlite_url_path(raw)


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
    APP_VERSION: str = "3.6.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    # B3 — scripted demo feed for the Command Bridge. Off by default, refused in production, and
    # every frame it serves is flagged `demo: true`.
    DEMO_MODE: bool = False

    HOST: str = "127.0.0.1"
    PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173"
    TRUSTED_PROXIES: str = ""

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
    AGENT_MAX_PARALLEL: int = Field(default=4, ge=1)
    AGENT_TASK_BUDGET_USD: float = Field(default=5.0, ge=0)
    SCHEDULER_INTERVAL_SECONDS: float = Field(default=5.0, gt=0)
    SELF_HEALING_INTERVAL_SECONDS: float = Field(default=60.0, gt=0)
    PROVIDER_MAX_RETRIES: int = Field(default=2, ge=0)
    PROVIDER_RETRY_AFTER_MAX_SECONDS: float = Field(default=30.0, ge=0)
    ANTHROPIC_API_KEY_REF: str = "env:ANTHROPIC_API_KEY"
    # Extra env names (comma separated) accepted as provider credential_ref besides *_API_KEY.
    # Server secrets are always rejected even when listed here.
    PROVIDER_CREDENTIAL_ENV_ALLOWLIST: str = ""
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
        # Phase 5: the default team runs DEFAULT_ACTIVE_WORKERS (4) employees in parallel.
        # ARENA_BRIDGE_MAX_CONCURRENCY is a per-profile limit, so it never caps the team below that.
        if "AGENT_MAX_PARALLEL" not in self.model_fields_set:
            self.AGENT_MAX_PARALLEL = max(self.DEFAULT_ACTIVE_WORKERS, self.ARENA_BRIDGE_MAX_CONCURRENCY)
        return self

    # Host-capacity ceiling for autoscaled workers; 0 = no ceiling (scale to every available profile).
    MAX_CONCURRENT_WORKERS: int = Field(default=25, ge=0)
    # Arena connector (Tampermonkey userscript in each employee's own Chrome profile <-> local bridge).
    # Controlled only by the connector switch and the Arena origins it may run on; there are no
    # authorization references, expirations, request intervals or usage quotas.
    ARENA_CONNECTOR_ENABLED: bool = False
    # Comma-separated https origins the userscript may run on (one @match per origin).
    ARENA_ALLOWED_ORIGINS: str = "https://arena.ai"
    # Arena Agent mode: optional URL of a fresh Agent session (empty = click the page's "New" button).
    ARENA_AGENT_NEW_SESSION_URL: str = ""
    # Keep Working continuations per task; 0 = unlimited.
    ARENA_AGENT_MAX_KEEP_WORKING: int = Field(default=0, ge=0)
    # Local mock Arena page for end-to-end testing at any time (e.g. http://127.0.0.1:4100/mock-arena/).
    ARENA_MOCK_URL: str = ""
    ARENA_REPLY_TIMEOUT_SECONDS: float = Field(default=600.0, gt=0)
    # How often the PR state of a session that waits for a human decision (reviewing /
    # awaiting_approval) is polled on GitHub: a PR merged or closed outside AATM must still be
    # noticed. 300 s = every 5 minutes.
    ARENA_PR_STATE_POLL_SECONDS: float = Field(default=300.0, gt=0)
    # R7: which runtime answers as manager-0. arena_agent = the manager's own Arena session (no API key);
    # anthropic_claude / openai_compatible = the first enabled provider of that type; demo = labelled
    # demo replies that never create tasks. Empty = the provider assigned to manager-0 (wizard / DB).
    AATM_MANAGER_PROVIDER: str = Field(default="", pattern="^(|arena_agent|anthropic_claude|openai_compatible|demo)$")
    # R7: the manager keeps one long-lived Arena session per project; after this many exchanges it starts a
    # new session (with a short context prompt). 0 = never rotate on count.
    ARENA_MANAGER_MAX_EXCHANGES: int = Field(default=40, ge=0)
    # Optional JSON object with the real Arena chat DOM selectors sent to the userscript as data.
    ARENA_DOM_SELECTORS: str = ""
    # Phase 5 - True parallelism / autoscaling
    DEFAULT_ACTIVE_WORKERS: int = Field(default=4, ge=1)
    AUTO_SCALE_MODE: str = Field(default="approval", pattern="^(approval|automatic|off)$")
    SCALE_UP_BACKLOG_THRESHOLD: int = Field(default=3, ge=1)
    SCALE_UP_WAIT_SECONDS: float = Field(default=120.0, ge=0)
    SCALE_DOWN_IDLE_MINUTES: float = Field(default=30.0, ge=0)
    SCALE_UP_STEP: int = Field(default=2, ge=1)
    # Per-provider in-flight limits, e.g. "anthropic-claude=4,openai=8" (unset = unlimited).
    PROVIDER_CONCURRENCY_LIMITS: str = ""
    SQLITE_EMPLOYEE_WARNING_THRESHOLD: int = 3
    # Message broker / coordination. auto: RabbitMQ if RABBITMQ_URL, else Postgres NOTIFY on Postgres, else memory.
    QUEUE_BACKEND: str = Field(default="auto", pattern="^(auto|rabbitmq|postgres|memory)$")
    RABBITMQ_URL: str = ""
    REDIS_URL: str = ""
    MAX_CONCURRENT_BROWSERS: int = 10
    # Phase 4: reviewer change-request rounds before the PR is escalated to a human.
    MAX_REVIEW_ROUNDS: int = Field(default=2, ge=0)
    STALLED_TASK_MINUTES: int = 15

    def worker_ceiling(self) -> int | None:
        """MAX_CONCURRENT_WORKERS as a ceiling, or None when it is 0 (no ceiling)."""
        return self.MAX_CONCURRENT_WORKERS or None

    def get_cors_origins(self) -> list[str]:
        origins = [o.strip() for o in (self.CORS_ORIGINS or "").split(",") if o.strip()]
        return origins or ["http://localhost:5173"]

    def get_trusted_proxies(self) -> set[str]:
        return {p.strip() for p in (self.TRUSTED_PROXIES or "").split(",") if p.strip()}

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
KNOWN_DEFAULT_PASSWORDS = frozenset(
    _PLACEHOLDER_PASSWORDS
    | {
        "changeme",
        "aatm-admin",
        "admin123",
        "password123",
        "123456",
        "12345678",
    }
)


def is_placeholder_password(password: str) -> bool:
    """Return True if `password` matches any known default/placeholder password."""
    raw = password or ""
    lowered = raw.strip().lower()
    return (
        raw in KNOWN_DEFAULT_PASSWORDS
        or lowered in KNOWN_DEFAULT_PASSWORDS
        or lowered.startswith("change-me")
        or lowered.startswith("change-this")
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
