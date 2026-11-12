"""PAY-2057 — the drain's candidate query skips batches a sweep is settling.

Before this, ``RetryScheduler.drain`` took its candidates straight from
``ReconciliationItemRepository.list_retryable_ids`` with no regard for a batch a sweep
held. Every candidate then failed ``try_acquire_batch_lock``, so a pass settled nothing
and the retryable backlog grew until the sweep ended.

The exclusion has to be in the candidate query, **before the ``LIMIT``**: filtering in
Python after the query still lets the swept batch's oldest items fill every slot. That is
the first thing asserted below, and it is why these tests use the real repository over
in-memory SQLite rather than a dictionary stub — the stub cannot show you where the
``LIMIT`` sits.

The engine is synchronous (``sqlite://`` + ``StaticPool``) behind a small async facade,
because ``aiosqlite`` is not a dependency here and this only needs ``await session.execute``
to work. Nothing here depends on advisory-lock semantics or isolation levels (ADR 0011's
concerns, covered against real Postgres in ``tests/integration``); the question is only
which rows one query returns. The lock manager and the poster are local stubs, so
``_claim_item``'s try-lock can be observed returning ``True`` and ``False`` without a
database that has ``pg_try_advisory_xact_lock``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.flags import StaticFeatureFlags
from app.models.base import Base
from app.models.reconciliation_item import ReconciliationItem
from app.models.reconciliation_run import ReconciliationRun
from app.models.settlement_batch import SettlementBatch
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.services.reconciliation.constants import MAX_ATTEMPTS, RETRYABLE_STATUSES
from app.services.reconciliation.poster import SettlementPoster
from app.services.reconciliation.retry import RetryScheduler
from app.services.reconciliation.types import SettlementResult
from tests.doubles import CollectingPublisher, FrozenClock
from tests.factories import make_batch, make_item

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 0, 15, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# A real repository over in-memory SQLite
# --------------------------------------------------------------------------------------


class AsyncSessionFacade:
    """The async surface the repository and the scheduler use, over a sync session.

    ``execute`` returns the synchronous ``Result`` directly: every caller here only uses
    the materialised methods (``all``, ``scalars``, ``scalar_one_or_none``), so awaiting
    the call is enough to make the repository's ``await session.execute(...)`` work.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instance: Any) -> None:
        self._session.add(instance)

    async def flush(self) -> None:
        self._session.flush()

    async def execute(self, statement: Any) -> Any:
        return self._session.execute(statement)

    async def get(self, model: Any, ident: Any) -> Any:
        return self._session.get(model, ident)

    async def rollback(self) -> None:
        self._session.rollback()

    async def commit(self) -> None:
        self._session.commit()


class SqliteSessions:
    """Commit on clean exit, roll back on raise — the production contract, in miniature."""

    def __init__(self, engine: Any) -> None:
        self._factory = sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSessionFacade]:
        session = self._factory()
        try:
            yield AsyncSessionFacade(session)
        except Exception:
            session.rollback()
            raise
        else:
            session.commit()
        finally:
            session.close()


@pytest.fixture
def sessions() -> Iterator[SqliteSessions]:
    """Three tables, one shared in-memory database, a session per ``begin()``.

    ``StaticPool`` is what makes every ``begin()`` see the rows another one committed; a
    default pool hands each connection its own blank database. Only the tables this query
    touches are created, because the rest of the schema carries Postgres-only JSONB.
    """
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [
        SettlementBatch.__table__,
        ReconciliationItem.__table__,
        ReconciliationRun.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    try:
        yield SqliteSessions(engine)
    finally:
        engine.dispose()


async def _seed(
    sessions: SqliteSessions,
    *,
    batch_id: str,
    due: dict[str, datetime],
    run_status: str | None = None,
) -> None:
    """One closed batch, one item per ``(id, next_attempt_at)``, and optionally a run."""
    async with sessions.begin() as session:
        session.add(make_batch(batch_id=batch_id, status="closed"))
        await session.flush()
        for item_id, next_attempt_at in due.items():
            session.add(
                make_item(
                    item_id=item_id,
                    batch_id=batch_id,
                    status="retryable",
                    next_attempt_at=next_attempt_at,
                )
            )
        if run_status is not None:
            session.add(
                ReconciliationRun(
                    id=f"rr_{batch_id}",
                    batch_id=batch_id,
                    trigger="scheduled",
                    status=run_status,
                    items_total=0,
                    items_settled=0,
                    items_failed=0,
                    started_at=NOW,
                )
            )


async def _candidates(sessions: SqliteSessions, *, limit: int) -> list[str]:
    async with sessions.begin() as session:
        return await ReconciliationItemRepository().list_retryable_ids(
            session, limit=limit, now=NOW
        )


# --------------------------------------------------------------------------------------
# The candidate query
# --------------------------------------------------------------------------------------


async def test_a_swept_batch_is_not_a_candidate(sessions: SqliteSessions) -> None:
    await _seed(
        sessions,
        batch_id="sb_swept",
        due={"ri_swept": NOW - timedelta(minutes=5)},
        run_status="running",
    )
    await _seed(sessions, batch_id="sb_free", due={"ri_free": NOW - timedelta(minutes=5)})

    assert await _candidates(sessions, limit=10) == ["ri_free"]


async def test_a_batch_whose_run_has_finished_is_still_a_candidate(
    sessions: SqliteSessions,
) -> None:
    await _seed(
        sessions,
        batch_id="sb_done",
        due={"ri_done": NOW - timedelta(minutes=5)},
        run_status="succeeded",
    )

    assert await _candidates(sessions, limit=10) == ["ri_done"]


async def test_the_exclusion_applies_before_the_limit(sessions: SqliteSessions) -> None:
    """The whole point of filtering in SQL.

    The swept batch's items are the oldest, so without the filter they take both slots and
    a pass with ``limit=2`` settles nothing. With it, the two slots go to work the drain
    can actually do.
    """
    await _seed(
        sessions,
        batch_id="sb_swept",
        due={
            "ri_swept_1": NOW - timedelta(minutes=9),
            "ri_swept_2": NOW - timedelta(minutes=8),
            "ri_swept_3": NOW - timedelta(minutes=7),
        },
        run_status="running",
    )
    await _seed(
        sessions,
        batch_id="sb_free",
        due={
            "ri_free_1": NOW - timedelta(minutes=2),
            "ri_free_2": NOW - timedelta(minutes=1),
        },
    )

    assert await _candidates(sessions, limit=2) == ["ri_free_1", "ri_free_2"]


async def test_the_backlog_still_shows_the_batch_a_sweep_is_holding(
    sessions: SqliteSessions,
) -> None:
    """``aggregate_backlog`` stays unfiltered on purpose (PAY-2057 design note).

    On-call needs to see the batch that isn't draining, not only the work the drain can
    take right now.
    """
    await _seed(
        sessions,
        batch_id="sb_swept",
        due={"ri_swept": NOW - timedelta(minutes=5)},
        run_status="running",
    )

    async with sessions.begin() as session:
        rows = await ReconciliationItemRepository().aggregate_backlog(
            session, statuses=RETRYABLE_STATUSES
        )

    assert [(row.batch_id, row.item_count) for row in rows] == [("sb_swept", 1)]


# --------------------------------------------------------------------------------------
# The drain end to end
# --------------------------------------------------------------------------------------


class RecordingLocks:
    """Advisory locks without Postgres. Can withhold a batch key on demand."""

    def __init__(self, *, withhold: set[str] | None = None) -> None:
        self.withhold = withhold or set()
        self.batch_attempts: list[str] = []

    async def try_acquire_batch_lock(self, session: Any, batch_id: str) -> bool:
        self.batch_attempts.append(batch_id)
        return batch_id not in self.withhold


class StubPoster(SettlementPoster):
    """``post_settlement`` without a ledger, and without an error path."""

    def __init__(self) -> None:
        self.settled: list[str] = []

    async def post_settlement(
        self, session: Any, item: Any, *, caller: str
    ) -> SettlementResult:
        self.settled.append(item.id)
        return SettlementResult(transaction_id=f"txn_{item.id}", created=True)


class StubSettings:
    reconcile_max_attempts = MAX_ATTEMPTS
    reconcile_retry_backoff_base_seconds = 30


def _scheduler(
    sessions: SqliteSessions, *, locks: RecordingLocks
) -> tuple[RetryScheduler, StubPoster]:
    poster = StubPoster()
    scheduler = RetryScheduler(
        sessions=sessions,  # type: ignore[arg-type]
        items=ReconciliationItemRepository(),
        poster=poster,
        publisher=CollectingPublisher(),
        clock=FrozenClock(NOW),
        flags=StaticFeatureFlags({}),
        locks=locks,  # type: ignore[arg-type]
        settings=StubSettings(),  # type: ignore[arg-type]
    )
    return scheduler, poster


async def test_drain_settles_the_free_batch_and_leaves_the_swept_one_alone(
    sessions: SqliteSessions,
) -> None:
    """Both sides of the ticket in one pass: skip the swept batch, drain the other."""
    await _seed(
        sessions,
        batch_id="sb_swept",
        due={
            "ri_swept_1": NOW - timedelta(minutes=9),
            "ri_swept_2": NOW - timedelta(minutes=8),
        },
        run_status="running",
    )
    await _seed(
        sessions,
        batch_id="sb_free",
        due={
            "ri_free_1": NOW - timedelta(minutes=2),
            "ri_free_2": NOW - timedelta(minutes=1),
        },
    )
    locks = RecordingLocks()
    scheduler, poster = _scheduler(sessions, locks=locks)

    settled = await scheduler.drain(limit=10)

    assert settled == 2
    assert poster.settled == ["ri_free_1", "ri_free_2"]
    # The swept batch is never even asked for its lock: one query made the decision.
    assert locks.batch_attempts == ["sb_free", "sb_free"]

    async with sessions.begin() as session:
        rows = (await session.execute(select(ReconciliationItem))).scalars().all()
        statuses = {item.id: item.status for item in rows}
    assert statuses == {
        "ri_swept_1": "retryable",
        "ri_swept_2": "retryable",
        "ri_free_1": "settled",
        "ri_free_2": "settled",
    }


async def test_the_try_lock_still_covers_the_race_with_the_sweep_start(
    sessions: SqliteSessions,
) -> None:
    """No run row read does not mean no sweep: the lock is still the exclusion.

    The sweep commits its run row before it takes the batch lock, and a run can close
    between the candidate query and the claim. This models the surviving window with a
    lock manager that refuses the batch, and asserts the item is left ``retryable`` for
    the next pass rather than settled by a second path (ADR 0011).
    """
    await _seed(sessions, batch_id="sb_racy", due={"ri_racy": NOW - timedelta(minutes=1)})
    locks = RecordingLocks(withhold={"sb_racy"})
    scheduler, poster = _scheduler(sessions, locks=locks)

    settled = await scheduler.drain(limit=10)

    assert settled == 0
    assert poster.settled == []
    assert locks.batch_attempts == ["sb_racy"]

    async with sessions.begin() as session:
        item = await ReconciliationItemRepository().get_or_raise(session, "ri_racy")
    assert item.status == "retryable"
    assert item.attempt_count == 0
