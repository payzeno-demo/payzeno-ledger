"""Session factories — the two :class:`~app.ports.SessionFactory` implementations.

**Binding rules, and the PAY-2043 fix depends on them** (`interfaces.md` §3.1):

1. Every call to :meth:`PooledSessionFactory.begin` checks out a **new** connection from
   the engine pool and opens its **own** transaction. Sessions are never nested onto one
   connection and never reused across calls. ``reconcile_batch`` calls ``begin()`` three
   times, nested (guard / read / per-item), and the two conflicting statements in
   `the-incident.md` §4 run in two different transactions on two different connections. A
   reentrant or scoped implementation either deadlocks or silently shares a transaction,
   and the defect evaporates.
2. ``expire_on_commit=False``. Objects returned from a committed block are read by the
   caller — ``retry_item`` returns its ``ReconciliationItem`` straight to a FastAPI route
   — and the default ``True`` makes that a ``DetachedInstanceError``.
3. **Nothing sets ``isolation_level``.** All sessions run at ``READ COMMITTED``. The
   hotfix's correctness argument is that a retry blocked on the batch advisory lock
   re-evaluates ``WHERE status IN RETRYABLE_STATUSES`` against data the sweep has since
   committed. Under ``REPEATABLE READ`` the snapshot is taken at the transaction's first
   statement — which after the fix is the advisory-lock ``SELECT`` — so the re-read would
   still see ``retryable`` and post a duplicate. The fix would look correct and not work.
4. ``DATABASE_POOL_SIZE >= 3 × concurrent sweeps + drains``. Production is **20**.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings

logger = logging.getLogger(__name__)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the asyncpg engine from :class:`Settings`.

    ``pool_pre_ping`` is on because ECS rotates tasks under us and a stale connection
    surfaces as a settlement failure rather than as a reconnect. ``pool_recycle`` is
    shorter than RDS's idle timeout for the same reason.

    Note the absence of ``isolation_level`` — see rule 3 in the module docstring. It is
    not an omission and it is asserted by ``tests/integration/test_session_isolation.py``.
    """
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=settings.database_echo,
        connect_args={"server_settings": {"application_name": "payzeno-ledger"}},
    )


class PooledSessionFactory:
    """The production :class:`~app.ports.SessionFactory`.

    One instance per process, constructed in ``app/container.py`` and shared by every
    service, repository, worker and consumer. It holds the engine; it does **not** hold a
    session, and neither does anything downstream of it.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = async_sessionmaker(
            bind=engine,
            expire_on_commit=False,
            autoflush=True,
            class_=AsyncSession,
        )

    @property
    def engine(self) -> AsyncEngine:
        """The underlying engine. Read by the readiness probe and by Alembic's runner."""
        return self._engine

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        """Open a new session on a **new** pooled connection, in its own transaction.

        Commits on clean exit, rolls back on any exception, and always returns the
        connection to the pool. Nesting is legal and is exactly what ``reconcile_batch``
        does — the inner call takes a second connection, which is why the pool has to be
        three times the sweep count.
        """
        session = self._sessionmaker()
        try:
            async with session.begin():
                yield session
        finally:
            await session.close()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        """A raw connection, for the trial-balance aggregates and the ops CLI.

        ``LedgerAuditService`` sums 41M entry rows per currency; going through the ORM
        identity map to do it is how the nightly job used to take eleven minutes.
        """
        async with self._engine.connect() as connection:
            yield connection

    async def dispose(self) -> None:
        """Close every pooled connection. Called from the FastAPI shutdown hook."""
        await self._engine.dispose()

    def pool_status(self) -> dict[str, Any]:
        """Pool gauges for ``/metrics`` and for the incident runbook.

        ``checked_out`` climbing to ``pool_size`` while the sweep is running is the
        signature of a drain convoying behind a batch lock (PAY-2057).
        """
        pool = self._engine.pool
        return {
            "size": getattr(pool, "size", lambda: 0)(),
            "checked_out": getattr(pool, "checkedout", lambda: 0)(),
            "overflow": getattr(pool, "overflow", lambda: 0)(),
        }


class SingleConnectionSessionFactory:
    """One connection, reused for every :meth:`begin`.

    The second :class:`~app.ports.SessionFactory` implementation, used by Alembic's
    ``run_migrations_online`` and by ``app/ops/cli.py``. Both want every statement on one
    connection — Alembic because a migration's DDL and its data backfill must share a
    transaction, the CLI because an operator running ``payzeno-ledger-ops`` against
    production should occupy exactly one connection slot and no more.

    **Never wire this into a service.** Sharing one connection across concurrent sessions
    serialises the sweep against itself and makes arc INC's interleaving impossible to
    reproduce — which is fine for a CLI and fatal for the ledger.
    """

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection
        self._sessionmaker = async_sessionmaker(
            bind=connection, expire_on_commit=False, class_=AsyncSession
        )

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        """Open a session on the single held connection, in a nested transaction."""
        session = self._sessionmaker()
        try:
            async with session.begin_nested():
                yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @property
    def connection(self) -> AsyncConnection:
        """The one connection every session in this factory shares."""
        return self._connection


@asynccontextmanager
async def single_connection_factory(
    engine: AsyncEngine,
) -> AsyncIterator[SingleConnectionSessionFactory]:
    """Check out one connection and wrap it in a :class:`SingleConnectionSessionFactory`.

    The entry point ``app/ops/cli.py`` uses; Alembic builds its own because it needs the
    connection before the event loop the CLI runs in exists.
    """
    async with engine.connect() as connection:
        yield SingleConnectionSessionFactory(connection)
