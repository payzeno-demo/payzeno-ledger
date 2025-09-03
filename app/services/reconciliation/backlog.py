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

        return set(running)


def _is_stale(oldest: datetime | None, now: datetime) -> bool:
    if oldest is None:
        return False
    return (now - oldest).total_seconds() > STALE_AFTER_SECONDS
