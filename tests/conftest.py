"""Root fixtures — shared by all seven test layers.

Two jobs.

**1. ``pg_engine`` — the real Postgres the suite did not have for nine months.** This
arrived with PAY-2053, after the incident, and its absence is the whole of §5.6 of the
postmortem. Everything above ``tests/integration/`` runs against
``tests/services/conftest.py``'s ``SharedSessionFactory``, which hands one session object
to every caller. That fixture is a faithful model of what this suite was, and it is
exactly why ``test_retry_is_idempotent`` and ``test_sweep_skips_settled_items`` **passed
on the buggy code**: one session is one transaction, so the second reader always sees the
first writer's uncommitted row, and a check-then-act guard looks airtight. Concurrency
cannot be expressed there at all — two connections is the entire premise of PAY-2041 — so
the regression test needed a real engine, and the real engine had to be here rather than
in ``tests/integration/`` because the repository layer needs real SQL too.

It is session-scoped: a testcontainer takes about four seconds to start and the
integration layer has enough tests that per-test containers were tried once and reverted.

**2. Event-loop and marker plumbing.** ``asyncio_mode = "strict"`` in ``pyproject.toml``
means every async test declares ``pytest.mark.asyncio`` (usually through a module-level
``pytestmark``), and ``--strict-markers`` means an unregistered marker is an error rather
than a silent skip.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

#: Set this to an existing database to skip the testcontainer entirely. CI does — it runs
#: a `postgres:15` service container — and so does anyone who got tired of waiting four
#: seconds for every `pytest -m integration`.
TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"

#: Postgres 15, matching production. Not `:latest`: `pg_advisory_xact_lock` semantics and
#: the `ON CONFLICT ... WHERE` planner behaviour under READ COMMITTED are the two things
#: this suite actually depends on, and neither is something to discover has changed.
POSTGRES_IMAGE = "postgres:15-alpine"


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """One loop for the whole session, so the session-scoped engine can outlive a test.

    pytest-asyncio's default is a loop per test, and an ``AsyncEngine`` created on a loop
    that has since closed fails on its next checkout with a bare ``RuntimeError: Event
    loop is closed`` that names nothing useful.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """A DSN for a live Postgres: an existing one, or a container we start.

    Skips — rather than fails — when neither is available. ``pytest -m "not integration"``
    is the gate every developer runs and it must not need Docker; ``pytest -m integration``
    is the second CI job and there it does.
    """
    existing = os.environ.get(TEST_DATABASE_URL_ENV)
    if existing:
        yield existing
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - dev extra not installed
        pytest.skip("testcontainers is not installed and TEST_DATABASE_URL is unset")

    try:
        with PostgresContainer(POSTGRES_IMAGE) as container:
            yield container.get_connection_url().replace(
                "postgresql+psycopg2://", "postgresql+asyncpg://"
            )
    except Exception as exc:  # pragma: no cover - no docker on this machine
        pytest.skip(f"cannot start {POSTGRES_IMAGE}: {exc}")


@pytest.fixture(scope="session")
async def pg_engine(postgres_dsn: str) -> AsyncIterator[AsyncEngine]:
    """The engine the integration and repository layers share.

    Note what is **not** set: ``isolation_level``. Every session runs at READ COMMITTED,
    the same as production, and that is load-bearing rather than incidental. The PAY-2043
    fix works because a retry blocked on the batch advisory lock re-evaluates ``WHERE
    status IN RETRYABLE_STATUSES`` against data the sweep has since committed. Under
    REPEATABLE READ the snapshot is taken at the transaction's first statement — which
    after the fix is the advisory-lock ``SELECT`` — so the re-read would still see
    ``retryable`` and post a duplicate. Pinning the isolation level here would make the
    regression test pass against a fix that does not work.

    ``pool_size`` is deliberately generous: ``reconcile_batch`` holds three sessions in one
    pass and the concurrency test runs a sweep and a drain at once.
    """
    engine = create_async_engine(
        postgres_dsn,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        echo=False,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def anyio_backend() -> str:
    """asyncio only. This service has no trio code path and never will."""
    return "asyncio"
