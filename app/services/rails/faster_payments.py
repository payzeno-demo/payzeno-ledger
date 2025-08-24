"""UK Faster Payments rail.

Near-instant, 24/7, but capped per transfer and settled through a sponsor bank. Above
the cap the payout is split — which the ledger does not model as multiple payouts, so
the split happens at the rail and only the aggregate reference comes back.
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.calendar import BankingCalendar
from app.errors import BankAccountUnusableError
from app.logging import get_logger
from app.models.payout import Payout
from app.models.projections import BankAccountProjection
from app.ports import Clock, InitiationResult
from app.services.payouts import PayoutInitiator

logger = get_logger(__name__)

#: Faster Payments per-transaction ceiling, in pence.
FPS_LIMIT_MINOR = 100_000_00


class FasterPaymentsPayoutInitiator(PayoutInitiator):
    """UK Faster Payments Scheme credit."""

    method: ClassVar[str] = "faster_payments"

    def __init__(self, settings: Settings, calendar: BankingCalendar, clock: Clock) -> None:
        self._settings = settings
        self._calendar = calendar
        self._clock = clock

    async def initiate(
        self, session: AsyncSession, payout: Payout, bank: BankAccountProjection
    ) -> InitiationResult:
        self._assert_usable(bank)
        if payout.currency != "GBP":
            raise BankAccountUnusableError(
                "Faster Payments is GBP only",
                bank_account_id=bank.bank_account_id,
                method=self.method,
                currency=payout.currency,
            )
        if not bank.sort_code_last_four:
            raise BankAccountUnusableError(
                "Faster Payments requires a sort code",
                bank_account_id=bank.bank_account_id,
                method=self.method,
                scheme=bank.scheme,
            )

        submitted_at = self._clock.now()
        # FPS clears on the same calendar day whenever the sponsor bank is open; the
        # cutoff still matters because our sponsor batches after 21:00 UTC.
        arrival = submitted_at.date()
        if submitted_at.time() >= self._settings.payout_cutoff_faster_payments_utc:
            arrival = self._calendar.next_business_day(arrival, payout.currency, self.method)

        parts = 1
        if payout.amount_minor > FPS_LIMIT_MINOR:
            parts = -(-payout.amount_minor // FPS_LIMIT_MINOR)
            logger.info(
                "faster_payments_split",
                payout_id=payout.id,
                amount_minor=payout.amount_minor,
                parts=parts,
            )

        rail_reference = f"FPS{payout.id[-16:].upper()}"

        # TODO(mhandover): wire the real SFTP drop once treasury signs off. The sponsor
        # bank's test endpoint accepts the file but does not acknowledge it, so we are
        # returning the reference and reconciling from the statement feed.
        logger.info(
            "faster_payments_payout_initiated",
            parts=parts,
            rail_reference=rail_reference,
            arrival_estimate=arrival,
        )
