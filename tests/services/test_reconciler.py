"""`ReconciliationService` — app/services/reconciliation/reconciler.py.

The sweep. Correct in isolation, which is the whole trouble: it serialises against other
sweeps with a batch-scoped advisory lock held in a dedicated guard transaction, and it takes
no row locks at all. Read this file next to test_retry.py and the lock domains do not
overlap — which is exactly what nobody did for three months.

Three shape assertions here are not decoration and are pinned individually:

* `_process_item` takes an ITEM ID and re-reads the row inside its own session. That re-read
  is step t1 of the interleaving.
* The counters are locals and `finish` takes them explicitly. `run` is loaded in one session
  and the loop runs in others; mutating it across them mutates a detached instance, and
  `items_total` (NOT NULL, consumed by `settlement.completed`) never gets set at all.
* `caller="batch_pass"` is passed from the very first version.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.db.locks import AdvisoryLockManager
from app.errors import OrphanedItemError, RetryableSettlementError
from app.services.reconciliation.constants import RETRYABLE_STATUSES
from app.services.reconciliation.poster import SettlementPoster
from app.services.reconciliation.reconciler import ReconciliationService
from app.services.reconciliation.types import SettlementResult
from tests.doubles import CollectingPublisher, FrozenClock
from tests.factories import make_batch, make_item

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 0, 15, tzinfo=UTC)


class RecordingLocks(AdvisoryLockManager):
    def __init__(self, *, grant: bool = True) -> None:
        self.grant = grant
        self.batch_locks: list[str] = []
        self.item_locks: list[str] = []
        self.row_locks: list[str] = []

    async def try_acquire_batch_lock(self, session: Any, batch_id: str) -> bool:
        if self.grant:
            self.batch_locks.append(batch_id)
        return self.grant

    async def post_settlement(self, session: Any, item: Any, *, caller: str) -> SettlementResult:
        self.calls.append((item.id, caller))
        if item.id in self.fail_with:
            raise self.fail_with[item.id]
        self._seq += 1
        return SettlementResult(transaction_id=f"txn_{self._seq:04d}", created=True)


class Settings:
    reconcile_max_items_per_run = 500
    publisher = CollectingPublisher()
    service = ReconciliationService(
        sessions=sessions_factory,
        locks=lock_manager,
        batches=batches,
        items=items,
        runs=runs,
        poster=poster,
        publisher=publisher,
        clock=FrozenClock(NOW),
        settings=Settings(),
    )
    return service, publisher, lock_manager


@pytest.fixture
def batch_of_three(batches, items):
    batches.seed(make_batch(batch_id="sb_sweep", status="closed"))
    for n in range(3):
        items.seed(
            make_item(
                item_id=f"ri_sweep_{n}",
                batch_id="sb_sweep",
                charge_id="ch_default",
                merchant_id="mer_default",
                status="retryable",
                next_attempt_at=NOW,
            )
        )
    return "sb_sweep"


async def test_reconcile_batch_settles_every_retryable_item(
    sessions_factory, batches, items, runs, batch_of_three
) -> None:
    run = await service.reconcile_batch(batch_of_three, trigger="scheduled")

    assert run.status == "succeeded"
    assert run.items_total == 3
    assert run.items_settled == 3
    assert run.items_failed == 0
    assert len(poster.calls) == 3


async def test_it_acquires_the_batch_advisory_lock(
    sessions_factory, batches, items, runs, batch_of_three
) -> None:
    poster = ScriptedPoster()
    service, _, locks = build(sessions_factory, batches, items, runs, poster)

    await service.reconcile_batch(batch_of_three)

    assert locks.batch_locks == [batch_of_three]


async def test_it_takes_no_row_locks_at_all(
    sessions_factory, batches, items, runs, batch_of_three
) -> None:
    """The other half of PAY-2041, asserted as an absence.

    The sweep's item read is a plain SELECT. Against another sweep the batch lock is
    sufficient. Against a drain holding `FOR UPDATE SKIP LOCKED` on the same row it is
    nothing at all, because SKIP LOCKED only skips rows somebody else has LOCKED.
    poster = ScriptedPoster()
    service, _, _ = build(sessions_factory, batches, items, runs, poster)

    await service.reconcile_batch(batch_of_three)

    assert {caller for _, caller in poster.calls} == {"batch_pass"}


async def test_process_item_takes_an_id_and_re_reads_the_row(
    sessions_factory, batches, items, runs, batch_of_three
) -> None:
    poster = ScriptedPoster(
        fail_with={"ri_sweep_1": RetryableSettlementError(item_id="ri_sweep_1", code="rate_limited")}
    )
    service, _, _ = build(sessions_factory, batches, items, runs, poster)

    run = await service.reconcile_batch(batch_of_three)

    assert items.rows["ri_sweep_0"].status == "failed"
    assert items.rows["ri_sweep_0"].last_error_code == "orphaned_item"
    assert run.items_failed == 1


async def test_mark_retryable_uses_its_own_session(
    sessions_factory, batches, items, runs, batch_of_three
) -> None:
    before = sessions_factory.begin_count

    await service.reconcile_batch(batch_of_three)

    assert sessions_factory.begin_count > before + 3
    assert items.rows["ri_sweep_0"].attempt_count == 1


async def test_max_items_bounds_the_pass(sessions_factory, batches, items, runs) -> None:
    batches.seed(make_batch(batch_id="sb_big", status="closed"))
    for n in range(20):
        items.seed(
            make_item(
                item_id=f"ri_big_{n:02d}",
                batch_id="sb_big",
                charge_id="ch_default",
                merchant_id="mer_default",
                status="retryable",
                next_attempt_at=NOW,
            )
        )
    run = await service.reconcile_batch("sb_big", max_items=5)

    assert run.items_total == 5
    assert len(poster.calls) == 5


async def test_run_counters_are_locals_not_orm_mutations(
    sessions_factory, batches, items, runs, batch_of_three
) -> None:
    poster = ScriptedPoster()
    service, _, _ = build(sessions_factory, batches, items, runs, poster)

    poster = ScriptedPoster(
        fail_with={"ri_sweep_0": RetryableSettlementError(item_id="ri_sweep_0", code="rate_limited")}
    )
    service, publisher, _ = build(sessions_factory, batches, items, runs, poster)

    await service.reconcile_batch(batch_of_three)

    assert "settlement.reconciliation_failed" in publisher.event_types()


async def test_an_empty_batch_still_produces_a_finished_run(
    sessions_factory, batches, items, runs
) -> None:
    batches.seed(make_batch(batch_id="sb_empty", status="closed"))
    run = await service.reconcile_batch("sb_empty")

    assert run.items_total == 0
    assert run.status == "succeeded"
    assert poster.calls == []


async def test_only_retryable_statuses_are_picked_up(
    sessions_factory, batches, items, runs
) -> None:
    batches.seed(make_batch(batch_id="sb_mixed", status="closed"))
    for status in ("pending", "retryable", "settled", "orphaned", "variance_exceeded"):
        items.seed(
            make_item(
                item_id=f"ri_{status}",
                batch_id="sb_mixed",
                charge_id="ch_default",
                merchant_id="mer_default",
                status=status,
                next_attempt_at=NOW,
            )
        )
    poster = ScriptedPoster()
    service, _, _ = build(sessions_factory, batches, items, runs, poster)

    run = await service.reconcile_batch("sb_mixed")

    assert run.items_total == len(RETRYABLE_STATUSES)
    assert {item_id for item_id, _ in poster.calls} == {"ri_pending", "ri_retryable"}
