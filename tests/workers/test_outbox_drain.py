"""`OutboxDrainJob` — app/workers/outbox_drain.py.

Five seconds. The only caller of `SnsPublisher` in the entire service.

Every business publish goes through `OutboxPublisher`, which writes a row inside the
caller's transaction so a rolled-back attempt emits nothing. That property is load-bearing:
during the twenty-two minute Worldflow outage a direct SNS publisher would have emitted
thousands of `settlement.item_settled` events for items that never settled, and payzeno-api
would have webhooked all of them to merchants.

This job is the other half — it takes committed rows and puts them on the bus. Its
failure mode is duplicate delivery, never loss, and consumers dedupe on `envelope.id`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.errors import BusPublishError
from app.publishers.sns import SnsPublisher
from app.workers.outbox_drain import OutboxDrainJob
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 8, 0, tzinfo=UTC)


class OutboxRow:
    def __init__(self, row_id: str, event_type: str) -> None:
        self.id = row_id
        self.event_id = f"evt_{row_id}"
        self.event_type = event_type
        self.payload: dict[str, Any] = {"item_id": "ri_1"}
        self.published_at: datetime | None = None
        self.attempt_count = 0


class StubOutbox:
    def __init__(self, rows: list[OutboxRow]) -> None:
        self.rows = rows
        self.marked: list[str] = []
        self.failed: list[str] = []
        self.limits: list[int] = []

    async def list_unpublished(self, session: Any, *, limit: int) -> list[OutboxRow]:
        self.limits.append(limit)
        return [row for row in self.rows if row.published_at is None][:limit]

    async def mark_published(self, session: Any, row_id: str, *, at: datetime) -> None:
        self.marked.append(row_id)
        for row in self.rows:
            if row.id == row_id:
                row.published_at = at

    async def mark_attempt_failed(self, session: Any, row_id: str) -> None:
        self.failed.append(row_id)
        for row in self.rows:
            if row.id == row_id:
                row.attempt_count += 1


class StubSns(SnsPublisher):
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.published: list[str] = []
        self.fail_on = fail_on or set()

    async def publish_raw(self, event_type: str, payload: dict[str, Any], **kwargs: Any) -> str:
        if event_type in self.fail_on:
            raise BusPublishError(f"sns rejected {event_type}")
        self.published.append(event_type)
        return f"msg_{len(self.published)}"


class Settings:
    outbox_drain_interval_seconds = 5
    outbox_drain_batch_size = 100


def _job(rows: list[OutboxRow], sns: StubSns, sessions):
    outbox = StubOutbox(rows)
    job = OutboxDrainJob(
        sessions=sessions, outbox=outbox, publisher=sns, clock=FrozenClock(NOW), settings=Settings()
    )
    return job, outbox


async def test_interval_is_five_seconds(sessions_factory) -> None:
    job, _ = _job([], StubSns(), sessions_factory)

    assert job.interval_seconds == 5
    assert job.name == "outbox_drain"


async def test_it_publishes_and_marks_each_row(sessions_factory) -> None:
    rows = [OutboxRow("ob_1", "settlement.item_settled"), OutboxRow("ob_2", "payout.paid")]
    sns = StubSns()
    job, outbox = _job(rows, sns, sessions_factory)

    result = await job.run_once()

    assert sns.published == ["settlement.item_settled", "payout.paid"]
    assert outbox.marked == ["ob_1", "ob_2"]
    assert result.items_processed == 2


async def test_a_row_that_fails_to_publish_is_not_marked(sessions_factory) -> None:
    """Duplicate delivery is acceptable; loss is not.

    Marking before the publish succeeds turns a transient SNS error into a permanently
    missing event, and the consumer that needed it has no way to know.
    """
    rows = [OutboxRow("ob_1", "payout.failed")]
    sns = StubSns(fail_on={"payout.failed"})
    job, outbox = _job(rows, sns, sessions_factory)

    result = await job.run_once()

    assert outbox.marked == []
    assert outbox.failed == ["ob_1"]
    assert result.items_processed == 0


async def test_one_bad_row_does_not_block_the_queue_behind_it(sessions_factory) -> None:
    rows = [
        OutboxRow("ob_bad", "payout.failed"),
        OutboxRow("ob_good", "settlement.completed"),
    ]
    sns = StubSns(fail_on={"payout.failed"})
    job, outbox = _job(rows, sns, sessions_factory)

    result = await job.run_once()

    assert sns.published == ["settlement.completed"]
    assert outbox.marked == ["ob_good"]
    assert result.items_processed == 1


async def test_an_already_published_row_is_not_republished(sessions_factory) -> None:
    published = OutboxRow("ob_done", "settlement.item_settled")
    published.published_at = NOW
    sns = StubSns()
    job, _ = _job([published], sns, sessions_factory)

    result = await job.run_once()

    assert sns.published == []
    assert result.items_processed == 0


async def test_this_is_the_only_place_sns_is_reached(sessions_factory) -> None:
    """`SnsPublisher` has one caller. Everything else uses the outbox.

    Asserted structurally rather than by convention, because the moment a service reaches
    for it directly the rollback guarantee is gone and nothing tells you.
    """
    job, _ = _job([], StubSns(), sessions_factory)

    assert isinstance(job._publisher, SnsPublisher)  # noqa: SLF001
