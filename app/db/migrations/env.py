"""
Alembic migration environment for async PostgreSQL (SQLAlchemy 2.0).

Supports both offline (SQL dump) and online (live DB) modes.
Uses AsyncEngine for Fluxito's async/await architecture.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Add project root to path so we can import app config
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# Import all ORM models so alembic autogenerate detects them
import app.models  # noqa: F401
from app.config import settings
from app.db.database import Base

config = context.config

# Inject DATABASE_URL from settings (never hardcode in alembic.ini)
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Load logging config from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Generates SQL script without connecting to the database.
    Useful for reviewing migrations before applying, or for
    CI/CD workflows that don't have direct DB access.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations against an open connection (called from async wrapper)."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations using async engine.

    Creates AsyncEngine, executes sync code in thread context,
    then cleans up resources. Used by start.sh and Fly release_command.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # No pooling for one-off migrations
    )

    async with connectable.connect() as connection:
        # Run sync migration code in async context via run_sync
        await connection.run_sync(do_run_migrations)

    # Dispose of the engine (close all connections)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run online migrations (default mode when DB is accessible)."""
    asyncio.run(run_async_migrations())


# Main entry point: choose offline or online based on context
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
