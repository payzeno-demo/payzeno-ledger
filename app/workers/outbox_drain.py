"""Transactional-outbox drain — the only caller of :class:`~app.publishers.sns.SnsPublisher`.

Every business path publishes through :class:`~app.publishers.outbox.OutboxPublisher`,
which INSERTs into ``event_outbox`` inside the caller's transaction. If that transaction
rolls back, the event rolls back with it. That property is the entire reason the outbox
exists: ``SettlementPoster`` publishes ``settlement.item_settled`` *before* its
transaction commits, and during an acquirer degradation a direct-to-SNS publisher would
emit thousands of phantom settlement events for items that never settled — each of which
payzeno-api would fan out to a merchant webhook.

This job is the other half. Every five seconds it takes the oldest unpublished rows and
hands them to SNS. Ordering is best-effort, not guaranteed: a failed publish is retried on
the next tick and its successors are not held back, because a single poison message must
not stall the whole bus. Consumers dedupe on ``event_id`` anyway
(``processed_event``, ADR 0011), so at-least-once with occasional reordering is the
contract downstream is written against.
"""

from __future__ import annotations

import time
from typing import ClassVar, Final

from app.config import Settings
from app.errors import BusPublishError
from app.logging import get_logger
from app.metrics import metrics
from app.ports import Clock, SessionFactory
from app.publishers.sns import SnsPublisher
from app.repositories.outbox import EventOutboxRepository
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

#: Five seconds. This is the floor on end-to-end event latency for the whole platform:
#: a merchant's webhook cannot fire before the ledger's event reaches SNS.
INTERVAL_SECONDS: Final[int] = 5

#: Rows per pass. At 5s that is 20/s sustained, comfortably above steady state and enough
#: to clear the burst a fully-reconciled batch produces within a couple of minutes.
BATCH_SIZE: Final[int] = 100

#: After this many failed publishes a row stops being retried and is left for the
#: ``LedgerOutboxStalled`` alarm and a human. Almost always a malformed payload rather
#: than an SNS problem, and retrying it forever just hides it.
MAX_PUBLISH_ATTEMPTS: Final[int] = 10


class OutboxDrainJob(PeriodicJob):
    """Publish staged ``event_outbox`` rows to SNS and mark them published."""

    name: ClassVar[str] = "outbox_drain"

    def __init__(
        self,
        sessions: SessionFactory,
        outbox: EventOutboxRepository,
        publisher: SnsPublisher,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._outbox = outbox
        self._publisher = publisher
        self._clock = clock
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS

    async def run_once(self) -> JobResult:
        """One drain pass.

        The read and the marks are in **separate transactions from the publish**. Holding
        a transaction open across an SNS round trip would pin a pooled connection for the
        length of a network call, and ``DATABASE_POOL_SIZE`` is 20 against four tasks
        already running three-session reconciliation passes.

        The consequence is that a crash between publish and mark re-publishes on the next
        tick. That is the correct trade: at-least-once with a dedupe key downstream, not
        at-most-once with a silently dropped settlement event.
        """
        started = time.monotonic()

        async with self._sessions.begin() as session:
            pending = await self._outbox.list_unpublished(
                session, limit=BATCH_SIZE, max_attempts=MAX_PUBLISH_ATTEMPTS
            )
            staged = [
                (row.id, row.event_type, row.body, row.merchant_id, row.correlation_id)
                for row in pending
            ]

        published = 0
        for row_id, event_type, body, merchant_id, correlation_id in staged:
            try:
                message_id = await self._publisher.publish_raw(
                    event_type=event_type,
                    body=body,
                    merchant_id=merchant_id,
                    message_id=message_id,
                    published_at=self._clock.now(),
                )
            published += 1
            metrics.increment("OutboxPublished", event_type=event_type)

        if staged:
            logger.info(
                "outbox_drain_pass",
                staged=len(staged),
                published=published,
                failed=len(staged) - published,
            )
        metrics.observe("OutboxBacklog", len(staged))
        return self._result(started, published)
