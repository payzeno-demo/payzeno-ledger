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
    poster = ScriptedPoster()
    service, _, _ = build(sessions_factory, batches, items, runs, poster)

