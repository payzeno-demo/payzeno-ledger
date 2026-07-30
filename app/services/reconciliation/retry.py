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
    ) -> ReconciliationItem | None:
        """Attempt one item. Returns ``None`` when the item was not claimable.

        A ``None`` is not an error. The route maps it onto ``409 settlement_locked`` and
        the console treats that as "already settling", refetches, and shows no toast.
        """
        try:
            async with self._sessions.begin() as session:
                item = await self._claim_item(session, item_id)
                if item is None:
                    return None
                if item.attempt_count >= self._settings.reconcile_max_attempts:
                    raise RetryExhaustedError(
                        f"item {item_id} exhausted {item.attempt_count} attempts",
                        item_id=item_id,
                        attempt_count=item.attempt_count,
                    )

                item.attempt_count += 1
                item.last_attempt_at = self._clock.now()
                item.status = "settling"

                result = await self._poster.post_settlement(
                    session, item, caller="retry_scheduler"
                )

                item.status = "settled"
                item.settled_transaction_id = result.transaction_id
                metrics.increment(
                    "SettlementItemRetried",
                    outcome="settled",
                    requested_by=requested_by or "unknown",
                )
                return item

        # Both branches run OUTSIDE the business session, in their own transaction,
        # because the business transaction has already rolled back and everything
        # written inside it is gone — including attempt_count. Mirrors
        # ReconciliationService._mark_retryable.
        except RetryableSettlementError as exc:
            await self._mark_retryable(item_id, exc.code)
            return None
        except RetryExhaustedError:
            await self._mark_failed(item_id, "retry_exhausted")
            return None
        except PayzenoLedgerError as exc:
            await self._mark_failed(item_id, exc.code)
            return None

    async def _mark_retryable(self, item_id: str, code: str) -> None:
        async with self._sessions.begin() as session:
            item = await self._items.get_or_raise(session, item_id)
            item.attempt_count += 1
            item.last_attempt_at = self._clock.now()
            item.last_error_code = code
            item.status = "retryable"
            item.next_attempt_at = self._backoff(item.attempt_count)
        metrics.increment("SettlementItemRetried", outcome="retryable", error_code=code)

    async def _mark_failed(self, item_id: str, code: str) -> None:
        async with self._sessions.begin() as session:
            item = await self._items.get_or_raise(session, item_id)
            item.attempt_count += 1
            item.last_attempt_at = self._clock.now()
            item.last_error_code = code
            item.status = "failed"
        metrics.increment("SettlementItemRetried", outcome="failed", error_code=code)

    async def _claim_item(
        self, session: AsyncSession, item_id: str
    ) -> ReconciliationItem | None:
        """Claim one item for this transaction, or return ``None``.

        Batch advisory lock first, row lock second. That ordering is the fix (PAY-2043)
        and it is unconditional since PAY-2056 removed the ``reconcile_batch_lock_on_retry``
        kill switch a week after the incident.

        Why it is sufficient: ``reconcile_batch`` holds the same
        ``pg_advisory_xact_lock(PAY, hash(batch_id))`` in a guard transaction that outlives
        every per-item transaction. A retry either wins the key and runs entirely, or fails
        to acquire and returns ``None``, leaving the item ``retryable`` for the next drain —
        by which time the sweep has committed ``settled`` and the ``status IN
        RETRYABLE_STATUSES`` predicate below excludes it. It depends on ``READ COMMITTED``:
        nothing in this service sets ``isolation_level``, and under ``REPEATABLE READ`` the
        snapshot would be taken at this very ``SELECT``, the re-read would still see
        ``retryable``, and the fix would look correct and not work.
        """
        batch_id = await self._items.get_batch_id(session, item_id)
        if batch_id is None:
            return None

        # NON-BLOCKING on purpose. A sweep can hold the batch lock for minutes across
        # 5,000 items; pg_advisory_xact_lock would park this drain worker on a pooled
        # connection for the whole pass, and 200 of those exhausts DATABASE_POOL_SIZE.
        # Failing fast leaves the item retryable for the next drain, which is exactly what
        # we want — the sweep is settling it anyway. The cost is the convoy mregression
        # raised on #171 at 02:52: during a sweep, every attempt in a drain pass fails and
        # throughput for that batch is zero. PAY-2057 is the fix and it is not done.
        if not await self._locks.try_acquire_batch_lock(session, batch_id):
            return None

        stmt = (
            select(ReconciliationItem)
            .where(ReconciliationItem.id == item_id)
            .where(ReconciliationItem.status.in_(RETRYABLE_STATUSES))
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def drain(self, *, limit: int) -> int:
        """Work through the retryable backlog, oldest ``next_attempt_at`` first.

        Serial on purpose: the whole point of the backoff column is to stop hammering a
        degraded acquirer, and firing ``limit`` retries concurrently would undo it.
        """
        async with self._sessions.begin() as session:
            item_ids = await self._items.list_retryable_ids(session, limit=limit)
        settled = 0
        for item_id in item_ids:
            if await self.retry_item(item_id, requested_by="retry_drain") is not None:
                settled += 1
        logger.info(
            "retry_drain_pass", candidates=len(item_ids), settled=settled, limit=limit
        )
        return settled

    def _backoff(self, attempt_count: int) -> datetime:
