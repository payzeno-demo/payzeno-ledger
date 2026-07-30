"""Read-only view of what is still waiting to settle.

Backs ``GET /internal/v1/reconciliation/backlog``, which the ops console polls and
which on-call reads during an acquirer degradation to answer the only question that
matters at 01:30: how much is stuck, in which batches, and how long has the oldest item
been waiting.

This is a reporting surface. It takes no locks, writes nothing, and never touches the
settlement path — which is also why it is the one place that can honestly report on a
batch that a sweep is currently holding.
"""

from __future__ import annotations

from datetime import datetime

from app.logging import get_logger
from app.ports import Clock, SessionFactory
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.reconciliation_run import ReconciliationRunRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.services.reconciliation.constants import RETRYABLE_STATUSES
from app.services.reconciliation.types import BacklogBucket

logger = get_logger(__name__)

#: A backlog older than this is what on-call actually wants highlighted.
STALE_AFTER_SECONDS = 900


class BacklogService:
    """Aggregates retryable items per batch."""

    def __init__(
        self,
        sessions: SessionFactory,
        items: ReconciliationItemRepository,
        batches: SettlementBatchRepository,
        runs: ReconciliationRunRepository,
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._items = items
        self._batches = batches
        self._runs = runs
        self._clock = clock

    async def get_backlog(
        self, *, currency: str | None = None, batch_id: str | None = None
    ) -> dict[str, object]:
        """Return the backlog grouped by batch, newest-blocking first.

        The response shape is the ``ReconciliationBacklog`` contract type: a total, a
        stale count, and one bucket per batch.
        """
        async with self._sessions.begin() as session:
            rows = await self._items.aggregate_backlog(
                session,
                statuses=RETRYABLE_STATUSES,
                currency=currency,
                batch_id=batch_id,
            )

            buckets: list[BacklogBucket] = []
            for row in rows:
                buckets.append(
                    BacklogBucket(
                        batch_id=row.batch_id,
                        currency=row.currency,
                        status=row.status,
                        item_count=row.item_count,
                        oldest_next_attempt_at=row.oldest_next_attempt_at,
                        gross_minor=row.gross_minor or 0,
                    )
                )

        now = self._clock.now()
        stale = sum(1 for bucket in buckets if _is_stale(bucket.oldest_next_attempt_at, now))
        total_items = sum(bucket.item_count for bucket in buckets)
        total_gross = sum(bucket.gross_minor for bucket in buckets)

        buckets.sort(key=lambda bucket: (-bucket.item_count, bucket.batch_id))

        logger.info(
            "reconciliation_backlog_read",
            batches=len(buckets),
            items=total_items,
            stale_batches=stale,
            currency=currency,
        )
        return {
            "total_items": total_items,
            "total_gross_minor": total_gross,
            "stale_batches": stale,
            "as_of": now.isoformat(),
            "buckets": [
                {
                    "batch_id": bucket.batch_id,
                    "currency": bucket.currency,
                    "status": bucket.status,
                    "item_count": bucket.item_count,
                    "gross_minor": bucket.gross_minor,
                    "oldest_next_attempt_at": (
                        bucket.oldest_next_attempt_at.isoformat()
                        if bucket.oldest_next_attempt_at
                        else None
                    ),
                }
                for bucket in buckets
            ],
        }

    async def batches_with_running_run(self, batch_ids: list[str]) -> set[str]:
        """Which of these batches currently have a ``running`` reconciliation run.

        Backed by ``pix_reconciliation_run_active``. Nothing on the settlement path
        calls this yet.

        TODO(PAY-2057): skip items whose batch has a running reconciliation_run instead
        of discovering it lock by lock. A drain pass loops up to 200 items serially and
        each one opens a transaction, asks for the batch lock, loses it to the sweep and
        returns None — 200 wasted round trips to learn a single fact this query answers
        once. Wiring it means RetryScheduler.drain needs the batch id per candidate,
        which list_retryable_ids does not return today.
        """
        if not batch_ids:
            return set()
        async with self._sessions.begin() as session:
            running = await self._runs.list_running_batch_ids(session, batch_ids)
        return set(running)


def _is_stale(oldest: datetime | None, now: datetime) -> bool:
    if oldest is None:
        return False
    return (now - oldest).total_seconds() > STALE_AFTER_SECONDS
