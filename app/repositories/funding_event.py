"""``funding_event`` data access — bank credits, ingested from the statement feed.

Cash follows the bank, not the file. ``settle`` posts on the strength of an acquirer's
settlement file; ``settlement_funding`` — the only posting that debits ``cash`` — posts
only when a real bank credit matches the batch within ``FUNDING_MATCH_TOLERANCE_BPS``.

Without this table the ledger would claim cash on the strength of a file alone, and an
acquirer that files a batch and then short-pays would leave Payzeno paying merchants out of
money that never arrived. ``PayoutCalculator.compute_available`` counts only funded
batches, which is what makes that guarantee reach the merchant.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.funding_event import FundingEvent
from app.repositories.base import BaseRepository


class FundingEventRepository(BaseRepository[FundingEvent]):
    """Reads and writes ``funding_event`` rows."""

    model: ClassVar[type[FundingEvent]] = FundingEvent
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return FundingEvent.id

    async def find_by_bank_reference(
        self, session: AsyncSession, bank_reference: str
    ) -> FundingEvent | None:
        """Look a credit up by the bank's own reference.

        Unique under ``uq_funding_event_bank_reference``, and that uniqueness is what makes
        re-ingesting a statement idempotent. Bank statement feeds re-send the whole day
        routinely; without the constraint, a re-send would fund every batch twice and debit
        ``cash`` for money that arrived once.
        """
        stmt = select(FundingEvent).where(FundingEvent.bank_reference == bank_reference)
        return (await session.execute(stmt)).scalars().first()

    async def list_unmatched(
        self,
        session: AsyncSession,
        *,
        limit: int = 200,
        acquirer: str | None = None,
        on_or_before: dt.date | None = None,
    ) -> list[FundingEvent]:
        """Credits that have not been placed against a batch, oldest value date first.

        Uses ``pix_funding_event_unmatched``. ``FundingMatchJob`` (900s) walks this and
        tries each against the unfunded batches for the same acquirer and currency.

        A credit that stays here is money in Payzeno's account that nobody can attribute,
        and finance chases it by hand. That is the correct behaviour — the alternative is
        guessing which merchant it belongs to.
        """
        stmt = (
            select(FundingEvent)
            .where(FundingEvent.status == "unmatched")
            .order_by(FundingEvent.value_date, FundingEvent.received_at)
            .limit(limit)
        )
        if acquirer is not None:
            stmt = stmt.where(FundingEvent.acquirer == acquirer)
        if on_or_before is not None:
            stmt = stmt.where(FundingEvent.value_date <= on_or_before)
        return list((await session.execute(stmt)).scalars().all())

    async def list_by_status(
        self,
        session: AsyncSession,
        *,
        statuses: tuple[str, ...],
        limit: int = 200,
    ) -> list[FundingEvent]:
        """Credits in any of ``statuses``.

        ``short_paid`` and ``disputed`` are the interesting ones: an acquirer paid less
        than it filed, or paid something Payzeno does not recognise. Both are finance
        conversations, and both leave ``processor_clearing`` outstanding, which is exactly
        what a receivable account is for.
        """
        if not statuses:
            return []
        stmt = (
            select(FundingEvent)
            .where(FundingEvent.status.in_(statuses))
            .order_by(FundingEvent.value_date.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def find_for_batch(
        self, session: AsyncSession, batch_id: str
    ) -> FundingEvent | None:
        """The credit that funded a batch, if one has."""
        stmt = select(FundingEvent).where(FundingEvent.matched_batch_id == batch_id)
        return (await session.execute(stmt)).scalars().first()

    async def mark_matched(
        self,
        session: AsyncSession,
        event_id: str,
        *,
        batch_id: str,
        variance_minor: int,
    ) -> FundingEvent:
        """Attach a credit to the batch it funded.

        ``variance_minor`` is recorded even when it is inside tolerance, because the
        pattern matters: an acquirer that is consistently eleven basis points light is a
        contract conversation, and it only shows up if the small variances are kept.
        """
        event = await self.get_or_raise(session, event_id)
        event.status = "matched"
        event.matched_batch_id = batch_id
        event.variance_minor = variance_minor
        await session.flush()
        return event

    async def mark_variance(
        self,
        session: AsyncSession,
        event_id: str,
        *,
        batch_id: str | None,
        variance_minor: int,
    ) -> FundingEvent:
        """Record a credit whose amount is outside tolerance.

        Short of the expected total is ``short_paid``; over it is ``disputed``, because
        an acquirer paying more than it filed is not a windfall, it is usually somebody
        else's money. Neither posts anything.
        """
        event = await self.get_or_raise(session, event_id)
        event.status = "short_paid" if variance_minor < 0 else "disputed"
        event.matched_batch_id = batch_id
        event.variance_minor = variance_minor
        await session.flush()
        return event

    async def sum_unmatched(
        self, session: AsyncSession, *, currency: str | None = None
    ) -> int:
        """Total value sitting unattributed. Exported as a gauge for finance."""
        stmt = (
            select(func.coalesce(func.sum(FundingEvent.amount_minor), 0))
            .where(FundingEvent.status == "unmatched")
        )
        if currency is not None:
            stmt = stmt.where(FundingEvent.currency == currency)
        return int((await session.execute(stmt)).scalar_one())
