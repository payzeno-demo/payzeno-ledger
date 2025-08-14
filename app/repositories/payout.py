"""``payout`` data access.

Two things in here are load-bearing and both are about money leaving the building:

* :meth:`PayoutRepository.sum_in_flight` reads rows a concurrent uncommitted transaction
  has not written yet. That is the identical check-then-act shape as PAY-2041, on the path
  that moves money to a bank account. ``PayoutService.create_payout`` therefore takes
  ``AdvisoryLockManager.acquire_merchant_currency_lock`` **before** computing the balance
  (advisory-then-row, ADR 0011), and ``pix_payout_in_flight`` — unique since migration
  ``0033`` — is the database's last word if that is ever bypassed.
* :meth:`PayoutRepository.mark_failed` and :meth:`PayoutRepository.mark_returned` both
  demand a ``reversal_transaction_id``. ``chk_payout_reversal_present`` enforces it at the
  storage layer as well, because ``failed`` is terminal and without a reversal the
  merchant's money is simply gone: the ``payout`` posting already debited
  ``merchant_payable`` and nothing would give it back.

This file went quiet about eight weeks ago when its author started handing over. It is
still correct; it is just nobody's, which is why the last two commits on it are a config
bump and a typo fix.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.payout import Payout
from app.repositories.base import BaseRepository

#: Statuses that still have money committed to them. Both are covered by
#: ``pix_payout_in_flight``, which is unique per (merchant, currency, livemode).
IN_FLIGHT_STATUSES: tuple[str, ...] = ("scheduled", "in_transit")


class PayoutRepository(BaseRepository[Payout]):
    """Reads and writes ``payout`` rows."""

    model: ClassVar[type[Payout]] = Payout
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return Payout.id

    async def sum_in_flight(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool = True,
    ) -> int:
        """Total minor units already committed to scheduled or in-transit payouts.

        Subtracted by ``PayoutCalculator.compute_available`` so a merchant cannot be paid
        the same balance twice. Note what this read cannot see: a payout another
        connection has inserted but not committed. That is why the calculator runs under
        the merchant-currency advisory lock, and why the partial index is unique.
        stmt = stmt.order_by(Payout.created_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def find_in_flight(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        """
        payout = await self.get_or_raise(session, payout_id)
        payout.status = "paid"
        payout.paid_at = paid_at
        payout.bank_reference = bank_reference
        await session.flush()
        return payout

    async def mark_failed(
        self,
        session: AsyncSession,
        payout_id: str,
        *,
        failure_code: str,
        failure_message: str,
        reversal_transaction_id: str,
