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
            payout_id=payout.id,
        )
