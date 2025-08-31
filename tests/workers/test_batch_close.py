"""`BatchCloseJob` — app/workers/batch_close.py.

Hourly. Closes batches that have been `open` longer than they should be.

Normally `SettlementImportService.import_file` closes its own batch at the end of the
import. This job exists for the case where it did not — the task was killed mid-import, or
the acquirer's file was truncated and the parse raised after the batch was created. An
`open` batch is invisible to the sweep, so without this the items sit there and a merchant
does not get paid, and the only symptom is a support ticket eleven days later.

Its guard is age, not count: an `open` batch whose `processing_date` is older than
`MAX_OPEN_HOURS` behind now is one nobody is still writing to.

The age comparison is at **date** granularity — `processing_date <= (now - 6h).date()` —
because `settlement_batch.processing_date` is the acquirer's processing day, not a
timestamp. That means the job only meaningfully distinguishes yesterday's batch from
today's, and it only does that while it runs before 06:00 UTC. It does (the scheduler
fires hourly and the interesting pass is the small-hours one), so the tests here are
written against a 03:00 clock, which is the world this job actually lives in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.errors import BatchNotReconcilableError
from app.workers.batch_close import BATCH_SIZE, MAX_OPEN_HOURS, BatchCloseJob
from tests.doubles import FrozenClock
from tests.factories import make_batch

pytestmark = pytest.mark.asyncio

#: 03:00 UTC — after the acquirers have filed and before the business day starts. The
#: cutoff this produces is 2026-04-15, so yesterday's batch is stale and today's is not.
NOW = datetime(2026, 4, 16, 3, 0, tzinfo=UTC)

YESTERDAY = date(2026, 4, 15)
TODAY = date(2026, 4, 16)


def _open_batch(batch_id: str, *, processing_date: date) -> Any:
    batch = make_batch(batch_id=batch_id, status="open")
    batch.processing_date = processing_date
    return batch


class StubBatches:
    def __init__(self, batches: list[Any]) -> None:
        self.batches = batches
        self.asked: list[tuple[str, ...]] = []

    async def list_by_status(self, session: Any, statuses: tuple[str, ...]) -> list[Any]:
        self.asked.append(statuses)
        return [batch for batch in self.batches if batch.status in statuses]


class StubSettlements:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.closed: list[str] = []
        self.fail_on = fail_on or set()

    async def close_batch(self, session: Any, batch_id: str) -> Any:
        if batch_id in self.fail_on:
            raise BatchNotReconcilableError("not open", batch_id=batch_id)
        self.closed.append(batch_id)
        return make_batch(batch_id=batch_id, status="closed")


class Settings:
    batch_close_interval_seconds = 3600


def _job(batches: list[Any], settlements: StubSettlements, sessions) -> BatchCloseJob:
    return BatchCloseJob(
        sessions=sessions,
        batches=StubBatches(batches),
        settlements=settlements,
        settings=Settings(),
        clock=FrozenClock(NOW),
    )


async def test_interval_is_hourly(sessions_factory) -> None:
    job = _job([], StubSettlements(), sessions_factory)

    assert job.interval_seconds == 3600
    assert job.name == "batch_close"


async def test_only_open_batches_are_considered(sessions_factory) -> None:
    settlements = StubSettlements()
    job = _job([], settlements, sessions_factory)

    await job.run_once()

    assert job._batches.asked == [("open",)]  # noqa: SLF001


async def test_a_stale_open_batch_is_closed(sessions_factory) -> None:
    settlements = StubSettlements()
    job = _job(
        [_open_batch("sb_stuck", processing_date=YESTERDAY)],
        settlements,
        sessions_factory,
    )

    result = await job.run_once()

    assert settlements.closed == ["sb_stuck"]
    assert result.items_processed == 1


async def test_a_batch_still_being_imported_is_left_alone(sessions_factory) -> None:
    """A 40,000-line Worldflow file takes a few minutes to parse and insert.

    Closing it halfway means the sweep starts settling a batch that is still growing, and
    `items_total` on the run row is wrong for the rest of time.
    """
    settlements = StubSettlements()
    job = _job(
        [_open_batch("sb_importing", processing_date=TODAY)], settlements, sessions_factory
    )

    result = await job.run_once()

    assert settlements.closed == []
    assert result.items_processed == 0


async def test_a_batch_someone_else_closed_first_is_not_an_error(sessions_factory) -> None:
    """The import finished between the list and the close. Normal, not a failure."""
    settlements = StubSettlements(fail_on={"sb_raced"})
    job = _job(
        [_open_batch("sb_raced", processing_date=YESTERDAY)],
        settlements,
        sessions_factory,
    )

    result = await job.run_once()

    assert result.error is None
    assert result.items_processed == 0


async def test_the_minimum_age_is_longer_than_a_slow_import(sessions_factory) -> None:
    """A 40,000-line file takes minutes. Six hours is not a close call."""
    assert MAX_OPEN_HOURS * 3_600 >= 1_800


async def test_a_pass_is_bounded(sessions_factory) -> None:
    """`BATCH_SIZE` caps a pass.

    Every close publishes `settlement.batch_closed` through the outbox and each one gets
    its own transaction, so an unbounded pass after a bad week is a long-running job
    holding a pooled connection for the whole of it.
    """
    settlements = StubSettlements()
    job = _job(
        [_open_batch(f"sb_{n:03d}", processing_date=YESTERDAY) for n in range(BATCH_SIZE + 10)],
        settlements,
        sessions_factory,
    )

    result = await job.run_once()

    assert len(settlements.closed) == BATCH_SIZE
    assert result.items_processed == BATCH_SIZE
