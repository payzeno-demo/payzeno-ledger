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

    async def retry_item(self, item_id: str, *, requested_by: str | None = None) -> Any:
        self.calls.append((item_id, requested_by))
        if self.raises is not None:
            raise self.raises
        return self.result


class StubReconciler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def reconcile_batch(
        self, batch_id: str, *, trigger: str = "scheduled", max_items: int = 5000
    ) -> Any:
        self.calls.append((batch_id, trigger, max_items))
        return _run(batch_id)


class StubLocks:
    """`AdvisoryLockManager.try_acquire_batch_lock` — the non-blocking probe.

    Records the session it was handed so a test can prove the probe ran in its own short
    transaction and not in the one the pass uses.
    """

    def __init__(self, *, acquired: bool = True) -> None:
        self.acquired = acquired
        self.sessions: list[Any] = []
        self.batch_ids: list[str] = []

    async def try_acquire_batch_lock(self, session: Any, batch_id: str) -> bool:
        self.sessions.append(session)
        self.batch_ids.append(batch_id)
        return self.acquired


class StubRunRepository:
    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.rows = rows or {}

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        return self.rows[entity_id]


class StubItemRepository:
    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.rows = rows or {}
        self.reads: list[str] = []

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str | None, str | None]] = []

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


# --------------------------------------------------------------------------------------
# POST /internal/v1/reconciliation/items/{itemId}/retry
# --------------------------------------------------------------------------------------


async def test_retry_returns_the_item_when_it_settles(sessions_factory) -> None:
    settled = make_item(
        item_id="ri_X", batch_id="sb_QK", status="settled", settled_transaction_id="txn_1"
    )
    scheduler = StubScheduler(result=settled)

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
    current = make_item(item_id="ri_X", batch_id="sb_QK", status="settling")
    scheduler = StubScheduler(result=None)

    with pytest.raises(SettlementLockedError) as excinfo:
        await retry_item(
            Body(requested_by="usr_ops_1"),
            sessions_factory,
            scheduler,
            StubRepositories(items=StubItemRepository({"ri_X": current})),
            CALLER,
            "ri_X",
        )

    assert excinfo.value.http_status == 409
    assert excinfo.value.code == "settlement_locked"


async def test_the_locked_response_carries_the_items_current_state(sessions_factory) -> None:
    """`error.details.item`. The console renders "already settling" from it.

    Current, not stale: the item is re-read in its own session after the claim failed, so
    the body describes the world the caller should now render rather than the world they
    clicked in. During an incident that saves a refetch per click on an already-busy
    service.
    """
    current = make_item(item_id="ri_X", batch_id="sb_QK", status="settled")
    items = StubItemRepository({"ri_X": current})
    scheduler = StubScheduler(result=None)

    with pytest.raises(SettlementLockedError) as excinfo:
        await retry_item(
            Body(requested_by=None),
            sessions_factory,
            scheduler,
            StubRepositories(items=items),
            CALLER,
            "ri_X",
        )

    assert excinfo.value.details["item_id"] == "ri_X"
    assert excinfo.value.details["item"]["status"] == "settled"
    assert items.reads == ["ri_X"]
    assert sessions_factory.begin_count == 1


async def test_retry_passes_requested_by_through(sessions_factory) -> None:
    """It lands on the metric label, and that is how we know a human did it.

    `requested_by='retry_drain'` versus a user id is the difference between "the drain is
    working" and "somebody is clicking the button during an incident".
    """
    scheduler = StubScheduler(result=make_item(item_id="ri_X", batch_id="sb_QK"))

    await retry_item(
        Body(requested_by="usr_ops_7"),
        sessions_factory,
        scheduler,
        StubRepositories(),
        CALLER,
        "ri_X",
    )

    assert scheduler.calls[0][1] == "usr_ops_7"


async def test_a_body_without_requested_by_falls_back_to_the_caller(sessions_factory) -> None:
    """`RetryReconciliationItemRequest` carries `requested_by?` only.

    The item id is bound by the path and is deliberately not repeated in the body. When
    the field is absent the internal caller identity stands in, so the label is never
    empty — an unattributed retry during an incident is the one you most want attributed.
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
    reconciler = StubReconciler()

    """
    locks = StubLocks()

    await start_run(
        Body(batch_id="sb_QK", trigger="manual", max_items=None),
        sessions_factory,
        locks,
        StubReconciler(),
        CALLER,
    )

    assert locks.batch_ids == ["sb_QK"]
    assert sessions_factory.begin_count == 1
    assert sessions_factory.sessions[0].committed is True


async def test_start_run_is_409_when_a_sweep_already_holds_the_batch(sessions_factory) -> None:
    """`try_acquire_batch_lock` returning False is `settlement_locked`, not a 500.

    An operator starting a manual run during a scheduled sweep is doing something
    reasonable and should be told so, not paged about. Blocking instead would park the
    request for the length of the pass and time out at the load balancer.
    """The runbook uses a small bound to reconcile one problem batch without a full pass."""
    reconciler = StubReconciler()

    await start_run(
        Body(batch_id="sb_7T", trigger="manual", max_items=50),
        sessions_factory,
        StubLocks(),
        reconciler,
        CALLER,
    )

    assert reconciler.calls == [("sb_7T", "manual", 50)]


async def test_a_missing_trigger_defaults_to_manual(sessions_factory) -> None:
    """Only the scheduler passes `scheduled`, and it does not come through this route."""
    reconciler = StubReconciler()

    await start_run(
        Body(batch_id="sb_QK", trigger=None, max_items=None),
        sessions_factory,
        StubLocks(),
        reconciler,
        CALLER,
    )

    assert reconciler.calls[0][1] == "manual"


# --------------------------------------------------------------------------------------
# GET /internal/v1/reconciliation/runs/{runId} and /backlog
# --------------------------------------------------------------------------------------


async def test_get_run_returns_the_run(sessions_factory) -> None:
    runs = StubRunRepository({"rr_000001": _run(status="succeeded")})

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
    """The ops dashboard tile. On the night of PAY-2041 it read 4,113."""
    backlog = StubBacklog({"total_items": 0, "buckets": [], "stale_batches": 0})

    payload = await get_backlog(backlog)

    assert payload["total_items"] == 0
    assert backlog.calls == [(None, None)]


async def test_the_backlog_route_touches_no_session() -> None:
    """It is polled every few seconds during an incident by everyone watching.

    `BacklogService` owns its own read; giving the route a session as well would double
    the connection cost of the one screen people refresh most.
    payload = await get_backlog(backlog, None, "sb_QK")

    assert payload["total_items"] == 12
    assert backlog.calls == [(None, "sb_QK")]
