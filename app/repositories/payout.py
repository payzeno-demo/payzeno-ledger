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
        limit: int = 500,
    ) -> list[Payout]:
        """Scheduled payouts whose ``available_on`` has arrived.

        ``PayoutSchedulerJob`` walks this hourly and hands each one to its rail.
        ``available_on`` is a banking-calendar date computed by
        ``BankingCalendar.next_business_day`` — not ledger booking time, which is a
        different thing and would pay merchants on bank holidays.
        """One merchant's payouts, newest first. Uses ``ix_payout_merchant_created``."""
        stmt = select(Payout).where(Payout.merchant_id == merchant_id)
        if status is not None:
            stmt = stmt.where(Payout.status == status)
        stmt = stmt.order_by(Payout.created_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def find_in_flight(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool = True,
    ) -> Payout | None:
        """The one in-flight payout for this merchant and currency, if any.

        There can be at most one, because ``pix_payout_in_flight`` is unique. The service
        checks this to return a friendly ``payout_blocked`` instead of letting the insert
        raise an ``IntegrityError`` the caller cannot interpret — but the check is a
        courtesy and the index is the guarantee, in that order.
        """Record that the rail accepted the instruction."""
        payout = await self.get_or_raise(session, payout_id)
        payout.status = "in_transit"
        payout.initiated_at = at
        if rail_reference is not None:
            payout.bank_reference = rail_reference
        await session.flush()
        return payout

    async def mark_paid(
        self,
        session: AsyncSession,
        payout_id: str,
        *,
        paid_at: dt.datetime,
        bank_reference: str,
    ) -> Payout:
        """Record settlement at the destination bank.

        ``paid`` is **not** terminal for ACH: an R-code return can arrive up to sixty days
        later, which is what :meth:`mark_returned` is for.
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
        at: dt.datetime | None = None,
    ) -> Payout:
        """Terminal failure, with the compensating posting that gives the money back.

        ``reversal_transaction_id`` is required and not optional. ``chk_payout_reversal_
        present`` will reject the row without it, and that constraint exists because the
        alternative — a terminal ``failed`` payout whose debit of ``merchant_payable`` is
        never reversed — destroys the merchant's balance permanently. Invariant (7) of
        `data-model.md` §6 checks it nightly.
        """
        payout = await self.get_or_raise(session, payout_id)
        payout.status = "failed"
        payout.failure_code = failure_code
        payout.failure_message = failure_message
        payout.reversal_transaction_id = reversal_transaction_id
        payout.failed_at = at or dt.datetime.now(dt.UTC)
        await session.flush()
        return payout

    async def mark_returned(
        self,
        session: AsyncSession,
        payout_id: str,
        *,
        failure_code: str,
        failure_message: str,
        reversal_transaction_id: str,
        """
        payout = await self.get_or_raise(session, payout_id)
        payout.status = "canceled"
        payout.updated_at = at
        await session.flush()
        return payout

    async def count_by_status(
        self, session: AsyncSession, *, merchant_id: str | None = None
    ) -> dict[str, int]:
