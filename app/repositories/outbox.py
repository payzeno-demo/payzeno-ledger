"""``event_outbox`` data access — the transactional outbox.

Every business-path publish is a row written here, in the **same transaction** as the work
it describes. If the transaction rolls back the event vanishes with it. That is not a
nicety: during the twenty-two minutes of PAY-2041 a direct SNS publisher would have emitted
thousands of ``settlement.item_settled`` events for items that never settled, and every
downstream webhook would have gone out.

``OutboxDrainJob`` (5s) is the only reader that publishes. It claims with
``FOR UPDATE SKIP LOCKED`` because the job runs in **all four** ECS tasks: an unclaimed
``WHERE published_at IS NULL`` select would publish every event four times.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.events import EventOutbox
from app.repositories.base import BaseRepository

#: Attempts before an event is dead-lettered. 2^12 seconds is a bit over an hour of
#: backoff in total, which is longer than any SNS outage we have actually had.
MAX_ATTEMPTS: int = 12

#: Backoff jitter, ±20%. Same rule as reconciliation retry and webhook delivery — stated
#: once in `domain-model.md` §8 and implemented three times, which is two too many.
JITTER_FRACTION: float = 0.2


class EventOutboxRepository(BaseRepository[EventOutbox]):
    """Stages, claims and completes outbox rows."""

    model: ClassVar[type[EventOutbox]] = EventOutbox
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return EventOutbox.id

    async def claim_due(
        self,
        session: AsyncSession,
        *,
        limit: int,
        claimed_by: str,
        now: dt.datetime | None = None,
    ) -> list[EventOutbox]:
        """Claim up to ``limit`` publishable events for this task.

        ``FOR UPDATE SKIP LOCKED``, ordered by ``occurred_at``, filtered on
        ``next_attempt_at <= now()`` and on neither published nor dead. Uses
        ``pix_event_outbox_due``.

        ``SKIP LOCKED`` rather than plain ``FOR UPDATE``: four drains blocking on each
        other would serialise the whole bus behind whichever task is slowest, and the
        events are independent. Skipping is correct — another task has that row and will
        publish it.

        The claim is stamped on the row (``claimed_at`` / ``claimed_by``) purely so an
        operator can see which task is sitting on a stuck event. Nothing reads it back.
        """
        stamp = now or dt.datetime.now(dt.UTC)
        stmt = (
            select(EventOutbox)
            .where(EventOutbox.published_at.is_(None))
            .where(EventOutbox.dead_at.is_(None))
            .where(EventOutbox.next_attempt_at <= stamp)
            .order_by(EventOutbox.occurred_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        for row in rows:
            row.claimed_at = stamp
            row.claimed_by = claimed_by
        await session.flush()
        return rows

    async def mark_published(
        self, session: AsyncSession, event_id: str, *, at: dt.datetime | None = None
    ) -> EventOutbox:
        """Record a successful SNS publish."""
        row = await self.get_or_raise(session, event_id)
        row.published_at = at or dt.datetime.now(dt.UTC)
        row.last_error = None
        await session.flush()
        return row

    async def mark_failed(
        self,
        session: AsyncSession,
        event_id: str,
        *,
        error: str,
        base_seconds: int = 2,
        now: dt.datetime | None = None,
    ) -> EventOutbox:
        """Record a failed publish and schedule the next attempt.

        ``attempt_count += 1``, ``next_attempt_at = now + base^attempt ± 20%``, and
        ``dead_at`` once :data:`MAX_ATTEMPTS` is reached.

        Without ``next_attempt_at`` a permanently failing event is re-selected every five
        seconds forever, in four tasks, and takes the drain's whole budget with it —
        which is how one malformed payload once delayed every other event in the system
        by nine minutes.
        """
        row = await self.get_or_raise(session, event_id)
        stamp = now or dt.datetime.now(dt.UTC)
        row.attempt_count += 1
        row.last_error = error[:1000]
        row.claimed_at = None
        row.claimed_by = None

        if row.attempt_count >= MAX_ATTEMPTS:
            row.dead_at = stamp
        else:
            delay = base_seconds**row.attempt_count
            jitter = delay * JITTER_FRACTION
            spread = random.uniform(-jitter, jitter)  # noqa: S311 (not cryptographic)
            row.next_attempt_at = stamp + dt.timedelta(seconds=max(1.0, delay + spread))
        await session.flush()
        return row

    async def list_dead(
        self, session: AsyncSession, *, limit: int = 100
    ) -> list[EventOutbox]:
        """Dead-lettered events, oldest first.

        Uses ``pix_event_outbox_dead``. The ops CLI prints these; requeueing one is
        :meth:`revive`, and it is a deliberate human act.
        """
        stmt = (
            select(EventOutbox)
            .where(EventOutbox.dead_at.is_not(None))
            .order_by(EventOutbox.occurred_at)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def revive(self, session: AsyncSession, event_id: str) -> EventOutbox:
        """Return a dead event to the queue, attempt counter reset.

        Operator-driven, from `docs/runbooks/reconciliation.md`. Publishing an event that
        died three days ago is safe because every consumer dedupes on
        ``(event_id, consumer)`` — which is the second reason ``processed_event`` has that
        key.
        """
        row = await self.get_or_raise(session, event_id)
        row.dead_at = None
        row.attempt_count = 0
        row.last_error = None
        row.next_attempt_at = dt.datetime.now(dt.UTC)
        await session.flush()
        return row

    async def count_pending(self, session: AsyncSession) -> int:
        """How many events are waiting to go out.

        Exported as a gauge. A pending count that climbs while the drain is running means
        SNS is refusing us, and it is the first graph anyone opens.
        """
        stmt = (
            select(func.count())
            .select_from(EventOutbox)
            .where(EventOutbox.published_at.is_(None))
            .where(EventOutbox.dead_at.is_(None))
        )
        return int((await session.execute(stmt)).scalar_one())

    async def purge_published_before(
        self, session: AsyncSession, *, before: dt.datetime
    ) -> int:
        """Delete published rows older than ``before``. Returns the row count.

        The outbox is a queue, not an audit log — the events themselves are on the bus and
        the business facts are in the tables the transaction wrote. Keeping published rows
        forever costs an index rebuild a month and buys nothing.
        """
        stmt = delete(EventOutbox).where(
            EventOutbox.published_at.is_not(None), EventOutbox.published_at < before
        )
        return int((await session.execute(stmt)).rowcount or 0)
