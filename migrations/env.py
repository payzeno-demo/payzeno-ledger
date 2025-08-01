"""Alembic's entry point.

Three things this file is responsible for and nothing else:

1. **Importing ``app.db.base``**, whose import side effect pulls in all twenty model
   classes and populates ``Base.metadata``. Without it ``alembic revision --autogenerate``
   produces an empty diff and cheerfully proposes dropping every table in the database.
2. **Resolving the URL from ``app.config.Settings``** and swapping the asyncpg driver for
   a synchronous one. ``alembic.ini`` deliberately leaves ``sqlalchemy.url`` empty: two
   sources of truth for a production DSN means one of them is wrong, and it is always the
   one in the file nobody edits.
3. **Running online migrations on a single connection**, which is the second consumer of
   ``SessionFactory``'s single-connection shape (``interfaces.md`` §3.4). A migration's
   DDL and its data backfill must share one transaction — ``0020`` quarantines 1,847
   duplicate rows and posts compensating reversals for them *in the same transaction* that
   creates the unique index, and split across connections that is not atomic.

``tests/integration/test_migrations.py`` drives ``upgrade head`` then ``downgrade base``
against a real Postgres and passes its own connection in through
``config.attributes["connection"]``, which is why the online path checks for one before
building an engine.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing app.db.base is what makes target_metadata non-empty. Do not "clean up" the
# unused-looking imports it performs — see its module docstring.
from app.config import Settings
from app.db.base import include_object, target_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """The DSN, from ``Settings``, with the async driver stripped.

    Alembic's runner is synchronous. Reading it here rather than from ``alembic.ini``
    keeps ``app/config.py`` the one env reader in the repository — including for the
    migration task, which runs as its own ECS task before the service rolls.
    """
    return Settings().database_url.replace("+asyncpg", "")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database.

    Used by ``make migrate-sql`` when a DBA wants to read what a release will do to a
    table with 41 million rows before it does it. ``0020``'s ``CREATE UNIQUE INDEX
    CONCURRENTLY`` in particular is not something anyone should meet for the first time in
    production.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure and run against one already-open synchronous connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
        # Every revision is hand-numbered NNNN and the chain is linear. Alembic's own
        # branch support is off because `data-model.md` §4 assigns an author per revision
        # number and the postmortem cites 0019 and 0020 by number — a merge revision
        # would make those citations ambiguous.
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Build an async engine, take exactly ONE connection, and run on it.

    One connection, not a pool: see the module docstring. The engine is disposed on the
    way out so a migration task exits rather than lingering on an idle connection while
    ECS waits for it.
    """
    section: dict[str, Any] = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = Settings().database_url

    connectable = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run against a live database.

    ``config.attributes["connection"]`` is how ``tests/integration/test_migrations.py``
    hands us its own connection: the test already holds one against a testcontainer and
    opening a second would be a different database session, so ``upgrade head`` followed
    by ``downgrade base`` could not be asserted as one story.
    """
    existing = config.attributes.get("connection", None)
    if existing is not None:
        do_run_migrations(existing)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
