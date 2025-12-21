"""``processed_event`` data access — inbound event deduplication.

The primary key is ``(event_id, consumer)`` and not ``event_id`` alone. Two consumers in
this service handle overlapping event sets, so with the id alone the second consumer to
see an event could never mark it processed and would reprocess it forever — or, worse,
would be silently skipped because the first consumer had already claimed it.

The claim is **insert-first**, in the same transaction as the handler's side effects.
There is deliberately no ``already_processed`` / ``mark_processed`` pair: check-then-act
around a side effect is exactly PAY-2041 and ADR 0011 forbids it in the money path. This
module is the smallest, clearest example of the pattern the incident taught, which is why
`docs/adr/0011-lock-ordering-in-the-money-path.md` quotes it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Insert

from app.errors import NotFoundError
from app.models.events import ProcessedEvent
from app.repositories.base import BaseRepository

#: How long a claim is kept. Long enough that no redelivery Amazon is capable of can
#: outlive it, short enough that the table stays small. Enforced by the retention job.
RETENTION_DAYS: int = 30


class ProcessedEventRepository(BaseRepository[ProcessedEvent]):
    """Claims event ids on behalf of one named consumer."""

    model: ClassVar[type[ProcessedEvent]] = ProcessedEvent
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return ProcessedEvent.processed_at

    def claim_statement(
        self, *, event_id: str, consumer: str, at: dt.datetime | None = None
    ) -> Insert:
        """Build the claim INSERT without executing it.

        Split out so the statement can be asserted in a unit test — the shape is the
        contract here, and a refactor that quietly turned it into a SELECT followed by an
        INSERT would still pass every behavioural test that runs on one connection. It
        would fail on two, at three in the morning.
        """
        return (
            pg_insert(ProcessedEvent)
            .values(
                event_id=event_id,
                consumer=consumer,
                processed_at=at or dt.datetime.now(dt.UTC),
            )
            .on_conflict_do_nothing(index_elements=["event_id", "consumer"])
            .returning(ProcessedEvent.event_id)
        )

    async def claim(
        self,
        session: AsyncSession,
        *,
        event_id: str,
        consumer: str,
        at: dt.datetime | None = None,
    ) -> bool:
        """Claim this event for this consumer. True when the claim is ours.

        ``INSERT ... ON CONFLICT DO NOTHING RETURNING event_id``, in the caller's
        transaction. False means somebody already processed it and the handler must not
        run at all.

        Because it is the caller's transaction, a handler that raises rolls the claim back
        with its side effects and the redelivery is processed cleanly. That is the whole
        design: the claim and the work are atomic together or neither happened.
        """
        result = await session.execute(
            self.claim_statement(event_id=event_id, consumer=consumer, at=at)
        )
        return result.scalar_one_or_none() is not None

    async def was_processed(
        self, session: AsyncSession, *, event_id: str, consumer: str
    ) -> bool:
        """Whether this consumer has already handled this event.

        A read, for the ops CLI and for support questions of the form "did we get it".
        **Not for the handler path** — deciding whether to do work from this is precisely
        the check-then-act the module docstring rules out. Use :meth:`claim`.
        """
        stmt = (
            select(ProcessedEvent.event_id)
            .where(ProcessedEvent.event_id == event_id)
            .where(ProcessedEvent.consumer == consumer)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def list_for_event(
        self, session: AsyncSession, event_id: str
    ) -> list[ProcessedEvent]:
        """Which consumers have processed one event.

        Answers "did the projection consumer see it but the posting consumer not", which
        is the first question support asks when a charge exists in the console and not in
        the ledger.
        """
        stmt = (
            select(ProcessedEvent)
            .where(ProcessedEvent.event_id == event_id)
            .order_by(ProcessedEvent.consumer)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def purge_older_than(
        self, session: AsyncSession, *, before: dt.datetime
    ) -> int:
        """Delete claims older than ``before``. Returns how many rows went.

        This and the outbox reclaim are the only deletes in ``payzeno_ledger``. Soft
        delete does not exist here and nothing else is ever removed.

        Thirty days is not arbitrary: it has to outlast the longest possible SQS
        redelivery plus the longest DLQ redrive somebody might run by hand after a bad
        deploy. Purging at seven days would mean a redriven week-old event is reprocessed
        as new, and for a ``payment.captured`` that is a second posting.
        """
        stmt = delete(ProcessedEvent).where(ProcessedEvent.processed_at < before)
        result = await session.execute(stmt)
        return int(result.rowcount or 0)
