"""US ACH rails.

Two initiators here because same-day ACH is a different NACHA window, a different
cutoff and a different fee, not a flag on the standard rail. They share the file
because they share the file format and the originator config.
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.calendar import BankingCalendar
from app.logging import get_logger
from app.models.payout import Payout
from app.models.projections import BankAccountProjection
from app.ports import Clock, InitiationResult
from app.services.payouts import PayoutInitiator

logger = get_logger(__name__)

ODFI_ROUTING_NUMBER = "021000021"


class AchPayoutInitiator(PayoutInitiator):
    """Standard next-day ACH credit."""

    method: ClassVar[str] = "ach"

    def __init__(self, settings: Settings, calendar: BankingCalendar, clock: Clock) -> None:
        self._settings = settings
        self._calendar = calendar
        self._clock = clock

    async def initiate(
        self, session: AsyncSession, payout: Payout, bank: BankAccountProjection
    ) -> InitiationResult:
        self._assert_usable(bank)
        submitted_at = self._clock.now()
        cutoff = self._settings.payout_cutoff_ach_utc
        effective = submitted_at.date()
        if submitted_at.time() >= cutoff:
            effective = self._calendar.next_business_day(effective, payout.currency, self.method)
        arrival = self._calendar.next_business_day(
            effective + timedelta(days=1), payout.currency, self.method
        )
        rail_reference = self._build_reference(payout, bank)
        logger.info(
            "ach_payout_initiated",
            payout_id=payout.id,
            rail_reference=rail_reference,
            submitted_at=submitted_at,
        )

    def _build_reference(self, payout: Payout, bank: BankAccountProjection) -> str:
        # NACHA trace number: 8-digit ODFI prefix + 7-digit sequence. We use the tail of
        # the payout ULID as the sequence because it is monotonic within a batch.
        sequence = "".join(ch for ch in payout.id if ch.isdigit())[-7:].rjust(7, "0")
        return f"{ODFI_ROUTING_NUMBER[:8]}{sequence}"


class SameDayAchPayoutInitiator(AchPayoutInitiator):
    """Same-day ACH credit.

    Three windows a day, a $1,000,000 per-transfer ceiling and a surcharge. Gated by
    the `payout_same_day_ach` flag, checked in PayoutService before we get here.
    """

    method: ClassVar[str] = "same_day_ach"

    SAME_DAY_LIMIT_MINOR: ClassVar[int] = 100_000_000

    async def initiate(
        self, session: AsyncSession, payout: Payout, bank: BankAccountProjection
    ) -> InitiationResult:
        self._assert_usable(bank)
        submitted_at = self._clock.now()
        if payout.amount_minor > self.SAME_DAY_LIMIT_MINOR:
            # Over the NACHA ceiling this silently becomes a next-day credit. Treasury
            # asked for the fallback rather than a hard failure.
            logger.warning(
                "same_day_ach_downgraded",
                payout_id=payout.id,
                amount_minor=payout.amount_minor,
            )
            return await AchPayoutInitiator.initiate(self, session, payout, bank)

        cutoff = self._settings.payout_cutoff_same_day_ach_utc
        arrival = submitted_at.date()
        if submitted_at.time() >= cutoff:
            arrival = self._calendar.next_business_day(arrival, payout.currency, self.method)
        rail_reference = f"SDA{self._build_reference(payout, bank)}"
        logger.info(
            "same_day_ach_payout_initiated",
            payout_id=payout.id,
            rail_reference=rail_reference,
            arrival_estimate=arrival.isoformat(),
        )
        return InitiationResult(
            rail_reference=rail_reference,
            submitted_at=submitted_at,
        )
