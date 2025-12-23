"""The batch sweep.

One pass over every eligible item in one settlement batch, serialised against other
sweeps of the same batch by a **batch-scoped Postgres advisory lock** (PAY-1402). The
lock lives in its own guard transaction that outlives every per-item transaction, so a
5,000-item pass never holds 5,000 rows' worth of locks.

Session usage in one pass, deliberately:

* ``guard``  — holds ``pg_advisory_xact_lock(PAY, hash(batch_id))`` for the pass
* ``read``   — lists the eligible items once
* per item   — one short transaction each, so a failure rolls back one item

That is three concurrent sessions from one shared repository instance, which is why
``BaseRepository`` is stateless and why ``DATABASE_POOL_SIZE`` is 20.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.locks import AdvisoryLockManager
from app.errors import PayzenoLedgerError, RetryableSettlementError
from app.logging import get_logger
from app.metrics import metrics
from app.models.reconciliation_run import ReconciliationRun
from app.ports import Clock, EventPublisher, SessionFactory
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.reconciliation_run import ReconciliationRunRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.services.reconciliation.constants import (
    COMPLETION_CHUNK_SIZE,
    RETRYABLE_STATUSES,
)
from app.services.reconciliation.poster import SettlementPoster
from app.services.reconciliation.types import ReconcilePassStats

if TYPE_CHECKING:  # pragma: no cover
    from app.config import Settings

logger = get_logger(__name__)


class ReconciliationService:
    """Reconciles a whole settlement batch."""

    def __init__(
        self,
        sessions: SessionFactory,
        locks: AdvisoryLockManager,
        batches: SettlementBatchRepository,
        items: ReconciliationItemRepository,
        runs: ReconciliationRunRepository,
        poster: SettlementPoster,
        publisher: EventPublisher,
        clock: Clock,
        settings: "Settings",
    ) -> None:
        self._sessions = sessions
        self._locks = locks
        self._batches = batches
        self._items = items
        self._runs = runs
        self._poster = poster
        self._publisher = publisher
        self._clock = clock
        # Read for `reconcile_sweep_wall_budget_seconds`, added by PR #171. Held as the
        # settings object rather than as an unpacked int so the budget can be changed
        # without a redeploy — which is the entire reason it is an env var.
        self._settings = settings

    async def reconcile_batch(
        self, batch_id: str, *, trigger: str = "scheduled", max_items: int = 5000
    ) -> ReconciliationRun:
        async with self._sessions.begin() as s:
            run = await self._runs.start(s, batch_id=batch_id, trigger=trigger)
        run_id = run.id

        stats = ReconcilePassStats()

        # A dedicated session held open for the whole pass, purely to hold the batch
        # advisory lock. Item work happens in its own short transactions so we never
        # hold 5000 rows' worth of locks (PAY-1402).
        async with self._sessions.begin() as guard:
            await self._locks.acquire_batch_lock(guard, batch_id)

            async with self._sessions.begin() as read_session:
                items = await self._items.list_for_settlement(
                    read_session,
                    batch_id=batch_id,
                    statuses=RETRYABLE_STATUSES,
                    limit=max_items,
                )
            stats.items_total = len(items)

            # Wall-clock budget for the pass (PR #171). The batch advisory lock is held
            # by `guard` for as long as this loop runs, and a 5,000-item pass at
            # 200-900ms per acquirer call held it for over an hour — which starved the
            # retry drain completely, since every `_claim_item` requests the same key.
            # A pass that runs over stops cleanly at the next item boundary and the rest
            # is picked up by the following sweep.
            budget_seconds = self._settings.reconcile_sweep_wall_budget_seconds
            started_monotonic = time.monotonic()

            for item in items:
                if time.monotonic() - started_monotonic > budget_seconds:
                    stats.items_total = stats.settled + stats.failed
                    logger.info(
                        "reconcile_pass_budget_exceeded",
                        batch_id=batch_id,
                        run_id=run_id,
                        budget_seconds=budget_seconds,
                        processed=stats.settled + stats.failed,
                        remaining=len(items) - (stats.settled + stats.failed),
                    )
                    break
                try:
                    async with self._sessions.begin() as session:
                        await self._process_item(session, item.id)
                    stats.settled += 1
                    stats.posted_total_minor += item.gross_minor
                    stats.fee_total_minor += item.fee_minor
                    stats.net_total_minor += item.net_minor
                    if item.charge_id is not None:
                        stats.settled_charge_ids.append(item.charge_id)
                except RetryableSettlementError as exc:
                    await self._mark_retryable(item.id, exc.code)
                    stats.failed += 1
                except PayzenoLedgerError as exc:
                    await self._mark_failed(item.id, exc.code)
                    stats.failed += 1
                    if exc.code == "orphaned_item":
                        stats.orphaned += 1

        async with self._sessions.begin() as s:
            run = await self._runs.finish(
                s,
                run_id,
                items_total=stats.items_total,
                items_settled=stats.settled,
                items_failed=stats.failed,
                status=stats.status,
                error_summary=None if stats.failed == 0 else f"{stats.failed} items failed",
            )
            await self._batches.mark_reconciled(
                s,
                batch_id,
                posted_total_minor=stats.posted_total_minor,
                fully_settled=stats.failed == 0,
                at=self._clock.now(),
            )

        await self._publish_completion(run, stats)
        metrics.increment(
            "ReconciliationRunFinished", trigger=trigger, status=stats.status
