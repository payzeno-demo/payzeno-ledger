"""`app/api/routers/reconciliation.py` — api-surface.md §10.3.

Four routes, and one of them is the route arc INC arrives through:

    POST /internal/v1/reconciliation/items/{itemId}/retry

It is reachable from the admin console (through payzeno-api's
`POST /v1/settlements/:batchId/items/:itemId/retry`) *and* from `RetryDrainJob`, and both
go through the same `RetryScheduler`. That sharing is not incidental — it is why a human
clicking "retry" during an incident can collide with a sweep, and it is why the route is
tested for what it does with the service's *return value* rather than for anything about
settlement itself. Every decision (is the item claimable, has it exhausted its attempts,
is the batch locked) belongs to the service. The route turns a value into a status code.

The `None` case gets four tests. `RetryScheduler.retry_item` returns
`ReconciliationItem | None`, and after PAY-2043 a `None` is the **normal** outcome of
losing the batch lock to a running sweep rather than an error. The route maps it onto
`409 settlement_locked` with the item's *current* state — re-read in its own session, not
the state the caller had when they clicked — under `error.details.item`. The console shows
"already settling" and refetches. Its rate is a signal, not an error budget burn.

`start_run`'s lock probe gets its own tests for a subtler reason: it is a pre-flight
check, not the lock. It opens a transaction, tries the batch lock, and lets the
transaction close — which releases it, because every lock in `app/db/locks.py` is
`pg_advisory_xact_lock`. `reconcile_batch` then takes the real one on its guard session.
A test that asserted the probe *held* the lock would be asserting a bug.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.api.routers.reconciliation import (
    get_backlog,
    get_run,
    retry_item,
    start_run,
)
from app.errors import ReconciliationItemNotFoundError, SettlementLockedError
from tests.factories import make_item

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 0, 15, tzinfo=UTC)

#: Who the internal-auth dependency says is calling. `retry_item` folds it into
#: `requested_by` when the body does not carry one, which is how a console click and a
#: drain pass end up distinguishable on the metric label.
CALLER = "payzeno-api"


class StubScheduler:
    """`RetryScheduler` seen from the route: one coroutine returning an item or `None`."""

    def __init__(self, *, result: Any = None, raises: Exception | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[tuple[str, str | None]] = []

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    body = await retry_item(
        Body(requested_by="usr_ops_1"),
        sessions_factory,
        scheduler,
        StubRepositories(),
        CALLER,
        "ri_X",
    )

    assert body["id"] == "ri_X"
    assert body["object"] == "reconciliation_item"
    assert body["status"] == "settled"
    assert scheduler.calls == [("ri_X", "usr_ops_1")]


async def test_a_settled_retry_never_opens_a_session(sessions_factory) -> None:
    """
    current = make_item(item_id="ri_X", batch_id="sb_QK", status="settled")
    """
    scheduler = StubScheduler(result=make_item(item_id="ri_X", batch_id="sb_QK"))

    await retry_item(
        Body(requested_by=None),
        sessions_factory,
        scheduler,
        StubRepositories(),
        CALLER,
        "ri_X",
    )

    assert scheduler.calls == [("ri_X", "console:payzeno-api")]


async def test_an_unknown_item_is_a_404_not_a_409(sessions_factory) -> None:
    """`None` plus a missing row means the id was wrong, not that the item was busy."""
    scheduler = StubScheduler(result=None)

    with pytest.raises(ReconciliationItemNotFoundError) as excinfo:
        await retry_item(
            Body(requested_by=None),
            sessions_factory,
            scheduler,
            StubRepositories(items=StubItemRepository({})),
            CALLER,
            "ri_ghost",
        )

    assert excinfo.value.http_status == 404


# --------------------------------------------------------------------------------------
# POST /internal/v1/reconciliation/runs
# --------------------------------------------------------------------------------------


async def test_start_run_returns_the_serialised_run(sessions_factory) -> None:
    body = await get_run(sessions_factory, StubRepositories(runs=runs), "rr_000001")

    assert body["id"] == "rr_000001"
    assert body["status"] == "succeeded"
    assert body["items_total"] == 4_113


async def test_get_run_reads_in_one_session(sessions_factory) -> None:
    runs = StubRunRepository({"rr_000001": _run()})

    await get_run(sessions_factory, StubRepositories(runs=runs), "rr_000001")

    assert sessions_factory.begin_count == 1


async def test_backlog_passes_its_filters_through() -> None:
    payload = await get_backlog(backlog, "USD", "sb_QK")

    assert payload["total_items"] == 4_113
    assert backlog.calls == [("USD", "sb_QK")]


async def test_backlog_with_no_filters_returns_everything() -> None:
