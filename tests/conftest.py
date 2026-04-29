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
@pytest.fixture(scope="session")
@pytest.fixture
