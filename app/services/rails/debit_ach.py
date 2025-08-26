"""ACH debit pull — the one rail that moves money *toward* Payzeno.

Used when a merchant goes negative: a chargeback lands after the payout has left, the
payable account is below zero, and the merchant has authorised us to recover it. This
is not a PayoutInitiator — nothing is being paid out — which is why it lives beside the
initiators rather than among them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.calendar import BankingCalendar
from app.domain.idempotency import ledger_key
from app.domain.postings import PostingLine
from app.errors import BankAccountUnusableError
from app.logging import get_logger
from app.models.projections import BankAccountProjection
from app.ports import Clock
from app.repositories.projections import BankAccountProjectionRepository
from app.services.transactions import LedgerPoster

logger = get_logger(__name__)

#: Never pull more than this in one instruction; anything larger goes to collections.
MAX_PULL_MINOR = 25_000_00


@dataclass(frozen=True, slots=True)
class DebitPullResult:
    rail_reference: str
    amount_minor: int
    effective_date: date
    submitted_at: datetime
    transaction_id: str


class AchPayoutPuller:
    """Originates an ACH debit against a merchant's verified bank account."""

    method = "debit_ach"

    def __init__(
        self,
        banks: BankAccountProjectionRepository,
        ledger: LedgerPoster,
        calendar: BankingCalendar,
        settings: Settings,
        clock: Clock,
    ) -> None:
        self._banks = banks
        self._ledger = ledger
        self._calendar = calendar
        self._settings = settings
        self._clock = clock

    async def pull(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        amount_minor: int,
        reason: str,
    ) -> DebitPullResult:
        if amount_minor <= 0:
            raise ValueError("debit pull amount must be positive")

        bank = await self._banks.get_default(
            session, merchant_id=merchant_id, currency=currency, livemode=True
        )
        self._assert_pullable(bank)

        capped = min(amount_minor, MAX_PULL_MINOR)
        if capped < amount_minor:
            logger.warning(
                "debit_pull_capped",
                merchant_id=merchant_id,
                requested_minor=amount_minor,
                capped_minor=capped,
            )

        submitted_at = self._clock.now()
        effective = self._calendar.next_business_day(
            submitted_at.date() + timedelta(days=1), currency, "ach"
        )
        rail_reference = f"DR{merchant_id[-10:].upper()}{effective.strftime('%m%d')}"

        # TODO(mhandover): wire the real SFTP drop once treasury signs off. Same drop as
        # sepa.py and faster_payments.py — one file, three rails, one credentials ticket
        # that has been open since month 6. Until then the reference above is synthetic
        # and the ledger side is the only side that happened: we post the entries, the
        # bank never sees a debit, and the merchant's negative balance is only cleared in
        # our books. Do not enable NegativeBalanceJob against live merchants on this.
        posted = await self._ledger.post(
            session,
            idempotency_key=ledger_key("debitpull", merchant_id, rail_reference),
            created_by="system",
            merchant_id=merchant_id,
            amount_minor=capped,
            amount_minor=capped,
            effective_date=effective,
            transaction_id=posted.transaction.id,
        )

    def _assert_pullable(self, bank: BankAccountProjection) -> None:
        if bank.status != "verified":
            raise BankAccountUnusableError(
                f"bank account {bank.bank_account_id} is {bank.status}",
                bank_account_id=bank.bank_account_id,
                status=bank.status,
                method=self.method,
            )
        if not bank.routing_last_four:
            raise BankAccountUnusableError(
                "ACH debit requires a routing number",
                bank_account_id=bank.bank_account_id,
                method=self.method,
                scheme=bank.scheme,
            )
