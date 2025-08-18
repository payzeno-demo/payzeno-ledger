"""``reserve_hold`` and ``banking_calendar`` data access.

Reserve is **released, not accumulated**. ``CapturePostingRule`` credits the ``reserve``
account when the merchant carries a ``reserve_bps`` and writes a hold row with
``release_on = capture_date + reserve_hold_days``; ``ReserveReleaseJob`` posts
``reserve_release`` on that date and stamps ``released_transaction_id`` here. Without the
release half, a merchant on a 10% rolling reserve accumulates money Payzeno can never pay
out and eventually notices.

The banking calendar reads live here too, because they are the same kind of thing — dates
the money is allowed to move on — and giving a five-column reference table its own
repository would be four files of ceremony for one query.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.reserve import BankingCalendarDay, ReserveHold
from app.repositories.base import BaseRepository


class ReserveHoldRepository(BaseRepository[ReserveHold]):
    """Reads and writes ``reserve_hold`` rows, and reads ``banking_calendar``."""

    model: ClassVar[type[ReserveHold]] = ReserveHold
    async def list_due(
        self,
        session: AsyncSession,
        *,
        on: dt.date,
        limit: int = 500,
    ) -> list[ReserveHold]:
        """Holds whose release date has arrived and which are still outstanding.

        Uses ``pix_reserve_hold_due``, partial on ``released_transaction_id IS NULL`` —
        the released rows are the overwhelming majority after a year and they have no
        business being in the index.

        The daily job releases these one transaction at a time rather than in a batch: a
        single failure in the middle of ninety releases must not roll back the eighty-nine
        that worked, because the next run would simply do them again and the idempotency
        key would reject them.
        """
        stmt = (
            select(ReserveHold)
            .where(ReserveHold.released_transaction_id.is_(None))
            .where(ReserveHold.release_on <= on)
            .order_by(ReserveHold.release_on)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_outstanding(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str | None = None,
    ) -> list[ReserveHold]:
        """Everything still held for one merchant, earliest release first.

        The console shows the sum as ``reserved_minor`` and the individual rows on the
        balance detail page, because "when do I get it back" is the first question a
        merchant on a reserve asks.
        """
        stmt = (
            select(ReserveHold)
            .where(ReserveHold.merchant_id == merchant_id)
            .where(ReserveHold.released_transaction_id.is_(None))
            .order_by(ReserveHold.release_on)
        )
        if currency is not None:
            stmt = stmt.where(ReserveHold.currency == currency)
        return list((await session.execute(stmt)).scalars().all())

    async def sum_outstanding(
        self, session: AsyncSession, *, merchant_id: str, currency: str
    ) -> int:
        """Total minor units currently held.

        Not subtracted by ``PayoutCalculator``: ``capture`` credited ``reserve`` instead
        of ``merchant_payable``, so the money was never in the payable balance and taking
        it off again would deduct the same reserve twice.
        """
        stmt = (
            select(func.coalesce(func.sum(ReserveHold.amount_minor), 0))
            .where(ReserveHold.merchant_id == merchant_id)
            .where(ReserveHold.currency == currency)
            .where(ReserveHold.released_transaction_id.is_(None))
        )
        return int((await session.execute(stmt)).scalar_one())

    async def mark_released(
        self, session: AsyncSession, hold_id: str, *, transaction_id: str
    ) -> ReserveHold:
        """Record the ``reserve_release`` posting that gave the money back.

        Stamped in the same transaction as the posting, which is what makes the job
        re-runnable: a hold with a transaction id is skipped, and a hold without one has
        definitely not been released, because the two commit together.
        """
        hold = await self.get_or_raise(session, hold_id)
        hold.released_transaction_id = transaction_id
        await session.flush()
        return hold

