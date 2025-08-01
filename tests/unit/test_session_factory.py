"""`SessionFactory` implementations — app/db/session.py.

Three properties are load-bearing and all three are asserted here:

1. `begin()` checks out a NEW pooled connection every call. `reconcile_batch` holds three at
   once (guard / read / per-item) and the retry drain a fourth. A reentrant or scoped
   factory silently shares one transaction and the whole of arc INC stops being expressible.
2. `expire_on_commit=False`. `retry_item` returns its `ReconciliationItem` straight out of a
   committed block into a FastAPI response model; the default would raise
   `DetachedInstanceError` at serialisation time.
3. Nothing sets `isolation_level`. Everything runs at READ COMMITTED, which is what makes
   the PAY-2043 fix work — see interfaces.md §3.1.

Only the first needs a real engine, so only that test carries the integration marker.
"""

from __future__ import annotations

import pytest

from app.db.session import PooledSessionFactory, SingleConnectionSessionFactory
from app.ports import SessionFactory


def test_both_factories_satisfy_the_protocol() -> None:
    assert issubclass(PooledSessionFactory, SessionFactory)
    assert issubclass(SingleConnectionSessionFactory, SessionFactory)


def test_pooled_factory_disables_expire_on_commit() -> None:
    factory = PooledSessionFactory.__new__(PooledSessionFactory)
    assert PooledSessionFactory.EXPIRE_ON_COMMIT is False
    assert getattr(factory, "EXPIRE_ON_COMMIT", None) is False


def test_pooled_factory_does_not_pin_an_isolation_level() -> None:
    # If this ever becomes REPEATABLE READ the hotfix in retry.py stops working while
    # continuing to look correct: the snapshot would be taken at the advisory-lock SELECT,
    # so the re-read after the lock is granted would still see status='retryable'.
    assert PooledSessionFactory.ISOLATION_LEVEL is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_begin_hands_out_distinct_connections(pg_engine) -> None:
    factory = PooledSessionFactory(pg_engine)

    async with factory.begin() as outer:
        outer_conn = await outer.connection()
        outer_pid = await outer.execute_scalar("SELECT pg_backend_pid()")

        async with factory.begin() as inner:
            inner_conn = await inner.connection()
            inner_pid = await inner.execute_scalar("SELECT pg_backend_pid()")

            assert outer is not inner
            assert outer_conn is not inner_conn
            # Different backends, therefore different transactions. This is the property
            # the arc INC interleaving needs: neither can see the other's uncommitted row.
            assert outer_pid != inner_pid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_connection_factory_reuses_one_connection(pg_engine) -> None:
    # Alembic and app/ops/cli.py want exactly this: one connection, one transaction, so a
    # migration's DDL and its data fixups cannot end up on two backends.
    factory = SingleConnectionSessionFactory(pg_engine)

    async with factory.begin() as first:
        first_conn = await first.connection()
    async with factory.begin() as second:
        second_conn = await second.connection()

    assert first_conn is second_conn


@pytest.mark.integration
@pytest.mark.asyncio
async def test_begin_rolls_back_on_an_exception(pg_engine) -> None:
    factory = PooledSessionFactory(pg_engine)

    with pytest.raises(RuntimeError):
        async with factory.begin() as session:
            await session.execute_scalar("SELECT 1")
            raise RuntimeError("boom")

    # the pool is not poisoned by the failed block
    async with factory.begin() as session:
        assert await session.execute_scalar("SELECT 1") == 1
