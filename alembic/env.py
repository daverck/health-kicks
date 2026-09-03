"""Alembic environment for the HealthKicks Cloud API (sync engine)."""

import logging
from logging.config import fileConfig
import os

from alembic import context
from alembic.migration import MigrationContext
from sqlalchemy import engine_from_config, inspect, pool

from app.core.config import settings
from app.db.models import Base

logger = logging.getLogger("alembic.env")

config = context.config
# Prioritize explicit environment variables or settings
database_url = (
    os.getenv("DATABASE_URL")
    or os.getenv("HEALTHKICKS_DATABASE_URL")
    or settings.database_url
)
config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

BASELINE_REVISION = "ac994f8fce7b"


def ensure_baseline_stamped_if_preexisting(connection) -> None:
    """If core tables exist (e.g. created by Base.metadata.create_all())
    without Alembic tracking, stamp the baseline migration (ac994f8fce7b)
    so subsequent migrations can run without colliding on existing tables."""
    existing_tables = set(inspect(connection).get_table_names())
    if "alembic_version" not in existing_tables and ("devices" in existing_tables or "users" in existing_tables):
        msg = (
            f"[alembic] Existing database schema detected without Alembic tracking. "
            f"Stamping baseline revision '{BASELINE_REVISION}' into alembic_version..."
        )
        print(msg)
        logger.warning(msg)
        ctx = MigrationContext.configure(connection)
        ctx.stamp(context.script, BASELINE_REVISION)
        if hasattr(connection, "commit"):
            connection.commit()


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        ensure_baseline_stamped_if_preexisting(connection)
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        if hasattr(connection, "commit"):
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
