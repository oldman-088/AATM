from logging.config import fileConfig
from typing import Any

import sqlalchemy as sa
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine.mock import MockConnection

import app.models  # noqa: F401
from alembic import context
from app.core.config import get_settings, normalize_sync_database_url
from app.db.base import Base


class _OfflineMockInspector:
    """Fallback inspector for Alembic `--sql` offline mode (`MockConnection`),
    where no live database connection exists to reflect existing columns/tables.
    """

    def __init__(self, _bind: Any) -> None:
        self.bind = _bind

    def get_columns(self, _table_name: str, **_kw: Any) -> list[dict[str, Any]]:
        return []

    def get_table_names(self, **_kw: Any) -> list[str]:
        return []

    def has_table(self, _table_name: str, **_kw: Any) -> bool:
        return False


if MockConnection not in sa.inspection._registrars:
    sa.inspection._inspects(MockConnection)(_OfflineMockInspector)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    override = config.get_main_option("sqlalchemy.url")
    if override:
        return normalize_sync_database_url(override)
    settings = get_settings()
    return settings.get_sync_database_url()


def run_migrations_offline() -> None:
    output_buffer = config.attributes.get("output_buffer")
    configure_kwargs = {
        "url": get_url(),
        "target_metadata": target_metadata,
        "literal_binds": True,
        "dialect_opts": {"paramstyle": "named"},
    }
    if output_buffer is not None:
        configure_kwargs["output_buffer"] = output_buffer
    context.configure(**configure_kwargs)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
