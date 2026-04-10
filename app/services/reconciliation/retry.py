"""Per-item retry (PAY-1607).

Before this existed, an item that failed a settlement attempt waited up to fifteen
minutes for the next batch sweep. Two entry points share this class: the internal
``POST /internal/v1/reconciliation/items/{itemId}/retry`` route, which the admin console
reaches through payzeno-api, and ``RetryDrainJob``, which drains the retryable backlog
every sixty seconds.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.locks import AdvisoryLockManager
from app.domain.backoff import next_attempt_at
from app.errors import (
    PayzenoLedgerError,
    RetryableSettlementError,
    RetryExhaustedError,
)
from app.logging import get_logger
from app.metrics import metrics
from app.models.reconciliation_item import ReconciliationItem
from app.ports import Clock, EventPublisher, FeatureFlags, SessionFactory
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.services.reconciliation.constants import MAX_ATTEMPTS, RETRYABLE_STATUSES
from app.services.reconciliation.poster import SettlementPoster

logger = get_logger(__name__)


class RetryScheduler:
    """Retries a single reconciliation item.

    Introduced in PAY-1607 so a transient processor failure no longer waits up to fifteen
    minutes for the next sweep.

    Concurrency: the item row lock in :meth:`_claim_item` excludes other ``RetryScheduler``
    callers, but it does **not** exclude ``ReconciliationService.reconcile_batch``, which
    serialises on a batch-scoped advisory lock and never row-locks the item. For nine
    months those were two disjoint mutual-exclusion mechanisms guarding one row, each
    correct against copies of itself and neither aware of the other — PAY-2041. Both paths
    now agree on the batch lock, taken **before** the row lock, everywhere. See
    ``docs/postmortems/2041-duplicate-settlement.md`` and
    ``docs/adr/0011-lock-ordering-in-the-money-path.md``.
    """

    def __init__(
        self,
        sessions: SessionFactory,
        items: ReconciliationItemRepository,
        poster: SettlementPoster,
        publisher: EventPublisher,
        clock: Clock,
        flags: FeatureFlags,
        locks: AdvisoryLockManager,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._items = items
        self._poster = poster
        self._publisher = publisher
        self._clock = clock
        self._flags = flags
        # Arrived with PAY-2043. It is why the hotfix could not be "one file, one
        # function": this constructor changed, and app/container.py changed with it.
        self._locks = locks
        self._settings = settings

    async def retry_item(
        self, item_id: str, *, requested_by: str | None = None
