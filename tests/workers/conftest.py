"""Fixtures for the eleven `PeriodicJob`s.

A job is a thin thing by design — read some rows, call a service, count what happened,
return a `JobResult` — and almost everything worth asserting about one is either its
interval or its refusal to do work it should not do. So the fixtures here are small.

Two of them are not, and both exist because of PAY-2041.

`JobSettings` is a plain object with every interval field on it rather than a real
`Settings` instance. That is not laziness: `Settings` is `pydantic-settings` and
constructing one reads the environment, which would make the interval assertions in this
directory depend on whoever's `.env` happened to be on the box. The property that
actually matters — **`interval_seconds` is a property backed by `Settings`, never a
class-body `os.environ.get`** — is asserted by mutating the settings object after the job
is constructed and watching the interval move. A frozen-at-import interval cannot do that,
and a frozen-at-import interval is the reason the 01:44 mitigation would have needed a
code change rather than a task-definition change. `tests/workers/test_registry.py` owns
that assertion for all eleven.

`WorkerSessionFactory` hands out one session per `begin()` and counts them. Job code opens
a session, reads a list, closes it, and then lets a service open its own — the count is
how a test tells the two apart, and it is how `test_reconcile_sweep.py` proves the sweep
does not hold a read transaction open across five thousand items.

I did ask whether these belonged in the root `conftest.py` instead. They do not: the
services layer wants a *shared* session and this layer wants a *counted* one, and one
fixture pretending to be both is how the pre-PAY-2053 suite got into trouble.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator

import pytest

from app.ports import SessionFactory

#: The clock every worker fixture agrees on. 00:15 UTC — the minute the first sweep of the
#: incident fired, which is a convenient thing to have in a traceback.
FIXED_NOW = datetime(2026, 4, 16, 0, 15, tzinfo=UTC)


class WorkerSession:
    """Stands in for `AsyncSession` inside a job pass."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.executed: list[str] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append(str(statement))
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class WorkerSessionFactory(SessionFactory):
    """A counted `SessionFactory`. The explicit base is the `IMPLEMENTS` edge.

    Every `begin()` produces a new `WorkerSession`, so `len(factory.sessions)` is the
    number of transactions a pass opened — the thing several tests in this directory are
    genuinely asserting, since "how many transactions did that take" is the difference
    between a job that scales and a job that holds a connection for an hour.
    """

    def __init__(self) -> None:
        self.sessions: list[WorkerSession] = []
        self.begin_count = 0

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[WorkerSession]:
        session = WorkerSession(self.begin_count)
        self.begin_count += 1
        self.sessions.append(session)
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()

    @property
    def session(self) -> WorkerSession:
        """The most recent session. Most jobs open exactly one."""
        if not self.sessions:
            raise AssertionError("this job never opened a session")
        return self.sessions[-1]


class JobSettings:
    """Every interval field, with its production default.

    Mutable on purpose. A test that wants to prove `interval_seconds` reads through to
    `Settings` sets the attribute after construction and asserts the job followed.
    """

    def __init__(self, **overrides: Any) -> None:
        self.reconcile_sweep_interval_seconds = 900
        self.reconcile_sweep_wall_budget_seconds = 30
        self.reconcile_max_items_per_run = 5_000
        self.reconcile_max_attempts = 6
        self.retry_drain_interval_seconds = 60
        self.retry_drain_batch_size = 200
        self.retry_drain_enabled = False
        self.settlement_import_interval_seconds = 3_600
        self.funding_match_interval_seconds = 900
        self.deferred_capture_interval_seconds = 30
        self.payout_scheduler_interval_seconds = 3_600
        self.reserve_release_interval_seconds = 86_400
        self.negative_balance_interval_seconds = 86_400
        self.ledger_audit_interval_seconds = 86_400
        self.outbox_drain_interval_seconds = 5
        self.outbox_drain_batch_size = 100
        self.batch_close_interval_seconds = 3_600
        self.batch_close_min_age_seconds = 900
        for key, value in overrides.items():
            if not hasattr(self, key):
                raise AssertionError(f"{key} is not a Settings field this layer reads")
            setattr(self, key, value)


class FixedClock:
    """`Clock` with no movement unless a test asks for it."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or FIXED_NOW

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


@pytest.fixture
def sessions_factory() -> WorkerSessionFactory:
    return WorkerSessionFactory()


@pytest.fixture
def job_settings() -> JobSettings:
    return JobSettings()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()
