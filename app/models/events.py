"""``processed_event`` and ``event_outbox`` — the bus's two storage halves.

**Inbound dedupe.** ``processed_event``'s primary key is ``(event_id, consumer)``, not
``event_id`` alone: two consumers in this service process overlapping type sets, so with
``event_id`` alone whichever ran second could never mark anything processed.
``BaseConsumer._claim_event`` claims it **insert-first**, in the same transaction as the
handler's side effects — ``INSERT … ON CONFLICT DO NOTHING RETURNING``, never
check-then-act. ADR 0011 forbids check-then-act in the money path, and this is the money
path.

**Outbound durability.** Every business-path publish writes ``event_outbox`` inside the
business transaction, so a rolled-back attempt emits nothing. During the 22-minute
Worldflow outage a direct SNS publisher would have produced thousands of phantom
``settlement.item_settled`` events for items that never settled.

``next_attempt_at`` is not optional: ``OutboxDrainJob`` runs every 5s in **all four**
tasks, and without a claim plus a backoff a permanently failing event is re-selected every
five seconds forever. The drain is::

    SELECT * FROM event_outbox
     WHERE published_at IS NULL AND dead_at IS NULL AND next_attempt_at <= now()
     ORDER BY occurred_at
       FOR UPDATE SKIP LOCKED
     LIMIT :n
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar, Final

from sqlalchemy import DateTime, Index, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, LivemodeMixin

#: `dead_at` is stamped at this attempt count. 2**12 seconds of backoff is over an hour;
#: an event that has failed twelve times is not going to publish on the thirteenth.
OUTBOX_MAX_ATTEMPTS: Final[int] = 12

#: Retention on `processed_event`, enforced by the maintenance job. Thirty days is longer
#: than any redrive window the DLQ consumer uses.
PROCESSED_EVENT_RETENTION_DAYS: Final[int] = 30


class ProcessedEvent(Base):
    """Inbound dedupe ledger. One row per (event, consumer) actually handled."""

    __tablename__ = "processed_event"
    entity_name: ClassVar[str] = "processed_event"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: The consumer's class name. In the key because PaymentEventConsumer and
    #: MerchantEventConsumer overlap on redriven envelopes.
    consumer: Mapped[str] = mapped_column(Text, primary_key=True)
    processed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_processed_event_processed_at", "processed_at"),)

    def age_days(self, now: dt.datetime) -> int:
        """How long this claim has been held. The retention job deletes past 30."""
        return (now - self.processed_at).days


class EventOutbox(Base, CreatedAtMixin, LivemodeMixin):
    """Transactional outbox. ``OutboxPublisher`` writes it; ``OutboxDrainJob`` drains it."""

    __tablename__ = "event_outbox"
    entity_name: ClassVar[str] = "event_outbox"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    #: Stamped from `payzeno_contracts.events.EVENT_SCHEMA_VERSIONS`. settlement.completed
    #: went to v2 with PAY-2052's chunking.
    version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    merchant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    causation_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    attempt_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    next_attempt_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Set by the drain's FOR UPDATE SKIP LOCKED claim, so an operator can see which task
    #: is sitting on a stuck event without reading pg_locks.
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    dead_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "pix_event_outbox_due",
            "next_attempt_at",
            postgresql_where="published_at is null and dead_at is null",
        ),
        Index("ix_event_outbox_type_occurred", "type", "occurred_at"),
        Index(
            "pix_event_outbox_dead",
            "dead_at",
            postgresql_where="dead_at is not null",
        ),
    )

    def is_due(self, now: dt.datetime) -> bool:
        """Whether the drain should pick this row up."""
        return (
            self.published_at is None and self.dead_at is None and self.next_attempt_at <= now
        )

    def is_exhausted(self) -> bool:
        """Whether the next failure should stamp ``dead_at``."""
        return self.attempt_count >= OUTBOX_MAX_ATTEMPTS

    def backoff_seconds(self) -> int:
        """``2**attempt_count`` seconds, capped at the dead threshold's exponent."""
        return 2 ** min(self.attempt_count, OUTBOX_MAX_ATTEMPTS)
