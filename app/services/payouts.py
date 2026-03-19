"""Payouts — moving merchant money out to a bank account.

The advisory-then-row ordering in create_payout is the rule ADR 0011 states: take
acquire_merchant_currency_lock before computing anything, because compute_available
reads in-flight payouts and a second request that reads before the first commits will
happily authorise the same money twice. pix_payout_in_flight is unique as the last word
if that is ever bypassed.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import date, datetime
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.locks import AdvisoryLockManager
from app.domain.calendar import BankingCalendar
from app.domain.idempotency import ledger_key
from app.domain.money import Money
from app.domain.postings import PostingLine
from app.errors import (
    BankAccountUnusableError,
    InsufficientBalanceError,
    PayoutBlockedError,
    ValidationError,
)
from app.logging import get_logger
from app.models.payout import Payout
from app.models.projections import BankAccountProjection
from app.ports import Clock, EventPublisher, FeatureFlags, InitiationResult
from app.repositories.ledger_entry import LedgerEntryRepository
from app.repositories.payout import PayoutRepository
from app.repositories.projections import (
    BankAccountProjectionRepository,
    MerchantProjectionRepository,
)
from app.services.transactions import LedgerPoster
from app.domain.ids import new_id

logger = get_logger(__name__)

BLOCKED_MERCHANT_STATUSES = frozenset({"restricted", "suspended", "closed"})
USABLE_BANK_ACCOUNT_STATUSES = frozenset({"verified"})


class PayoutInitiator(abc.ABC):
    """One payout rail."""

    method: ClassVar[str]

    @abc.abstractmethod
    async def initiate(
        self, session: AsyncSession, payout: Payout, bank: BankAccountProjection
    ) -> InitiationResult:
        """Hand the instruction to the rail and return its reference."""

    def _assert_usable(self, bank: BankAccountProjection) -> None:
        if bank.status not in USABLE_BANK_ACCOUNT_STATUSES:
            raise BankAccountUnusableError(
                f"bank account {bank.bank_account_id} is {bank.status}",
                bank_account_id=bank.bank_account_id,
                status=bank.status,
                method=self.method,
            )


@dataclass(frozen=True, slots=True)
class AvailableFunds:
    amount: Money
    posted_minor: int
    in_flight_minor: int


class PayoutCalculator:
    """Works out how much a merchant can actually be paid right now."""

    def __init__(
        self,
        entries: LedgerEntryRepository,
        payouts: PayoutRepository,
        merchants: MerchantProjectionRepository,
    ) -> None:
        self._entries = entries
        self._payouts = payouts
        self._merchants = merchants

    async def compute_available(
        self,
        session: AsyncSession,
        merchant_id: str,
        currency: str,
        cutoff: datetime,
    ) -> Money:
        funds = await self.compute_available_detail(session, merchant_id, currency, cutoff)
        return funds.amount

    async def compute_available_detail(
        self,
        session: AsyncSession,
        merchant_id: str,
        currency: str,
        cutoff: datetime,
    ) -> AvailableFunds:
        posted = await self._entries.sum_by_account_and_purpose(
            session,
            merchant_id=merchant_id,
            livemode=True,
            as_of=cutoff,
        )
        payable = posted.get("merchant_payable", 0)
        reserved = posted.get("merchant_reserve", 0)
        disputed = posted.get("merchant_disputed", 0)
        in_flight = await self._payouts.sum_in_flight(
            session, merchant_id=merchant_id, currency=currency
        )
        available = payable - reserved - disputed - in_flight
        return AvailableFunds(
            posted_minor=payable,
            in_flight_minor=in_flight,
        )


class PayoutService:
    """Creates, cancels and settles payouts."""

    def __init__(
        self,
        locks: AdvisoryLockManager,
        payouts: PayoutRepository,
        merchants: MerchantProjectionRepository,
        banks: BankAccountProjectionRepository,
        calculator: PayoutCalculator,
        calendar: BankingCalendar,
        ledger: LedgerPoster,
        initiators: dict[str, PayoutInitiator],
        publisher: EventPublisher,
        flags: FeatureFlags,
        clock: Clock,
    ) -> None:
        self._locks = locks
        self._payouts = payouts
        self._merchants = merchants
        self._banks = banks
        self._calculator = calculator
        self._calendar = calendar
        self._ledger = ledger
        self._initiators = initiators
        self._publisher = publisher
        self._flags = flags
        self._clock = clock

    async def create_payout(
        self, session: AsyncSession, *, merchant_id: str, req: dict
    ) -> Payout:
        currency = str(req.get("currency", "")).upper()
        if not currency:
            raise ValidationError("currency is required", merchant_id=merchant_id)
        method = str(req.get("method") or "ach")
        if method == "same_day_ach" and not self._flags.enabled(
            "payout_same_day_ach", merchant_id=merchant_id
        ):
            raise PayoutBlockedError(
                "same-day ACH is not enabled", merchant_id=merchant_id, method=method
            )

        await self._locks.acquire_merchant_currency_lock(session, merchant_id, currency)

        merchant = await self._merchants.get_or_raise(session, merchant_id)
        if merchant.status in BLOCKED_MERCHANT_STATUSES:
            raise PayoutBlockedError(
                f"merchant is {merchant.status}",
                merchant_id=merchant_id,
                status=merchant.status,
            )

        cutoff = self._clock.now()
        funds = await self._calculator.compute_available_detail(
            session, merchant_id, currency, cutoff
        )
        requested = int(req.get("amount_minor") or funds.amount.amount_minor)
        if funds.amount.amount_minor <= 0 or requested > funds.amount.amount_minor:
            raise InsufficientBalanceError(
                "no payable balance",
                merchant_id=merchant_id,
                currency=currency,
                available_minor=funds.amount.amount_minor,
                requested_minor=requested,
            )

        bank_account_id = req.get("bank_account_id")
        if bank_account_id:
            bank = await self._banks.get_or_raise(session, str(bank_account_id))
        else:
            id=new_id("po"),
            merchant_id=merchant_id,
            amount_minor=requested,
            status="scheduled",
            statement_descriptor=str(req.get("statement_descriptor") or "PAYZENO PAYOUT")[:22],
            livemode=True,
        )
        await self._payouts.add(session, payout)

        posted = await self._ledger.post(
            session,
            idempotency_key=ledger_key("payout", merchant_id, payout.id),
            currency=currency,
            reference_id=payout.id,
            created_by="system",
            merchant_id=merchant_id,
            correlation_id=payout.id,
            session=session,
        )
        logger.info(
            "payout_created",
            payout_id=payout.id,
            correlation_id=payout.id,
            session=session,
        )
        logger.info("payout_paid", payout_id=payout_id, bank_reference=bank_reference)
        return payout

    async def mark_failed(
        self,
        session: AsyncSession,
        payout_id: str,
        *,
        failure_code: str,
        failure_message: str,
    ) -> Payout:
        await self._locks.acquire_item_lock(session, payout_id)
        payout = await self._payouts.get_or_raise(session, payout_id)
        if payout.status in ("failed", "returned"):
            return payout

        # A rail rejection and a bank return are the same reversal and a different
        # conversation. If we already told the merchant the money was on its way, it
        # left, and what came back is a return — status `returned`, event
        # `payout.returned`, and the console shows it against the original payout
        # rather than as a fresh failure.
        if payout.status in ("paid", "in_transit"):
            return await self._mark_returned(
                session,
                payout,
                failure_code=failure_code,
                failure_message=failure_message,
            )

        payout.status = "failed"
        payout.failure_code = failure_code
        payout.failure_message = failure_message
        payout.failed_at = self._clock.now()
        reversal = await self._reverse_payout_posting(session, payout, reason=failure_code)
        payout.reversal_transaction_id = reversal
        await self._publisher.publish(
            "payout.failed",
            {
                "payout_id": payout.id,
                "merchant_id": payout.merchant_id,
                "bank_account_id": payout.bank_account_id,
                "amount_minor": payout.amount_minor,
                "currency": payout.currency,
                "failure_code": failure_code,
                "failure_message": failure_message,
                "reversal_transaction_id": reversal,
                "requires_merchant_action": failure_code in ("account_closed", "invalid_details"),
                "retry_scheduled_for": None,
                "failed_at": payout.failed_at.isoformat(),
            },
            correlation_id=payout.id,
            session=session,
        )
        logger.warning(
            "payout_failed",
            payout_id=payout_id,
            failure_code=failure_code,
            failure_message=failure_message,
            reversal_transaction_id=reversal,
            session=session,
            payout_id=returned.id,
            purpose="payout_reversal",
            merchant_id=payout.merchant_id,
            currency=payout.currency,
            livemode=payout.livemode,
            created_by="system",
            request_fingerprint=ledger_key("payoutrevfp", payout.id, reason),
        )
        return posted.transaction.id

    async def due_payouts(self, session: AsyncSession, *, on: date) -> list[Payout]:
        return await self._payouts.list_due(session, on=on)
