"""`BacklogService` — app/services/reconciliation/backlog.py.

The read model on-call opens at 01:30. Its whole job is answering "how much is stuck,
where, and for how long" without taking a lock, so the assertions here are mostly about
what it does *not* do: no locks, no writes, and it stays honest about a batch that a
sweep is currently holding.

`batches_with_running_run` is tested even though nothing on the settlement path calls it.
That is PAY-2057 — the ticket `dhotfix` filed at 02:52 in response to `mregression`'s
lock-convoy objection on PR #171 — and the query is the finished half. Wiring it into
`RetryScheduler.drain` needs the batch id per candidate, which `list_retryable_ids` does
not return, and that is where it stopped. Testing the finished half means the day someone
picks the ticket up, they only have to write the drain side.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.reconciliation.backlog import STALE_AFTER_SECONDS, BacklogService
from app.services.reconciliation.constants import RETRYABLE_STATUSES
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 1, 30, tzinfo=UTC)


class StubItems:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def aggregate_backlog(
        self,
        session: Any,
        *,
        statuses: frozenset[str],
        currency: str | None,
        batch_id: str | None,
    ) -> list[Row]:
        self.calls.append(
            {"statuses": statuses, "currency": currency, "batch_id": batch_id}
        )
        rows = self.rows
        if currency is not None:
            rows = [row for row in rows if row.currency == currency]
        if batch_id is not None:
            rows = [row for row in rows if row.batch_id == batch_id]
        return rows


class StubBatches:
    pass


def _incident_rows() -> list[Row]:
    """The two batches from the night of PAY-2041, at their peak."""
    return [
        Row("sb_QK", "USD", "partially_reconciled", 2_704, NOW - timedelta(hours=1), 27_040_000),
        Row("sb_7T", "GBP", "partially_reconciled", 1_409, NOW - timedelta(minutes=50), 14_090_000),
        Row("sb_calm", "USD", "closed", 3, NOW - timedelta(seconds=30), 30_000),
    ]


async def test_backlog_sorts_the_biggest_batch_first(sessions_factory) -> None:
    """On-call reads the first line and stops. It had better be the worst one."""
    service, _, _ = _service(_incident_rows(), sessions=sessions_factory)

    backlog = await service.get_backlog()

    assert [bucket["batch_id"] for bucket in backlog["buckets"]] == [
        "sb_QK",
        "sb_7T",
        "sb_calm",
    ]


async def test_backlog_counts_stale_batches(sessions_factory) -> None:
    service, _, _ = _service(_incident_rows(), sessions=sessions_factory)

    backlog = await service.get_backlog()

    # Two batches older than the sweep interval; the third is 30 seconds old.
    assert backlog["stale_batches"] == 2
    assert STALE_AFTER_SECONDS == 900


async def test_backlog_only_looks_at_retryable_statuses(sessions_factory) -> None:
    """The same constant the sweep and the drain import. One definition of eligible."""
    service, items, _ = _service(_incident_rows(), sessions=sessions_factory)

    await service.get_backlog()

    assert items.calls[0]["statuses"] == RETRYABLE_STATUSES


async def test_backlog_filters_by_currency(sessions_factory) -> None:
    service, _, _ = _service(_incident_rows(), sessions=sessions_factory)

    backlog = await service.get_backlog(currency="GBP")

    assert [bucket["batch_id"] for bucket in backlog["buckets"]] == ["sb_7T"]
    assert backlog["total_items"] == 1_409


async def test_backlog_filters_by_batch(sessions_factory) -> None:
    service, _, _ = _service(_incident_rows(), sessions=sessions_factory)

    backlog = await service.get_backlog(batch_id="sb_QK")

    assert backlog["total_items"] == 2_704


async def test_backlog_never_writes_and_never_locks(sessions_factory) -> None:
    """A reporting surface that takes the batch lock cannot report during an incident.

    The one moment anyone reads this endpoint is while a sweep is holding every lock it
    can find. It has to be readable then.
    """
    service, _, _ = _service(_incident_rows(), sessions=sessions_factory)

    await service.get_backlog()

    assert sessions_factory.session.committed is True
    assert sessions_factory.session.rolled_back is False
    assert sessions_factory.session.executed == []


# --------------------------------------------------------------------------------------
# PAY-2057 — the finished half
# --------------------------------------------------------------------------------------


async def test_batches_with_running_run_reports_the_blocked_batches(sessions_factory) -> None:
    service, _, runs = _service(
        _incident_rows(), running=["sb_QK"], sessions=sessions_factory
    )

    blocked = await service.batches_with_running_run(["sb_QK", "sb_7T", "sb_calm"])

    assert blocked == {"sb_QK"}
    assert runs.asked == [["sb_QK", "sb_7T", "sb_calm"]]


async def test_nothing_on_the_settlement_path_calls_it_yet(sessions_factory) -> None:
    """PAY-2057, asserted as the open question it is.

    `RetryScheduler.drain` still discovers a running sweep lock by lock: 200 items, 200
    transactions, 200 failed acquisitions, one fact. Closing this needs the batch id per
    candidate and `list_retryable_ids` returns bare ids. When someone changes that, this
    test is where the new behaviour goes.
    """
    from app.services.reconciliation import retry

    source = retry.__file__
    with open(source, encoding="utf-8") as handle:
        body = handle.read()

    assert "batches_with_running_run" not in body
