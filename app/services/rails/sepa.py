"""SEPA Credit Transfer rail.

Euro payouts inside the SEPA zone. The instruction is a pain.001 XML message dropped on
treasury's SFTP endpoint; what is built here is the message identity and the value date,
which is what the ledger needs to record.
"""

from __future__ import annotations

from datetime import timedelta
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

SEPA_SCHEME = "sepa_credit_transfer"


class SepaPayoutInitiator(PayoutInitiator):
    """SEPA Credit Transfer."""

    method: ClassVar[str] = "sepa"

    def __init__(self, settings: Settings, calendar: BankingCalendar, clock: Clock) -> None:
        self._settings = settings
        self._calendar = calendar
        self._clock = clock

    async def initiate(
        self, session: AsyncSession, payout: Payout, bank: BankAccountProjection
    ) -> InitiationResult:
        self._assert_usable(bank)
        if not bank.iban_last_four:
            raise BankAccountUnusableError(
                "SEPA requires an IBAN",
                bank_account_id=bank.bank_account_id,
                method=self.method,
                scheme=bank.scheme,
            )
        if payout.currency != "EUR":
            raise BankAccountUnusableError(
                "SEPA Credit Transfer is EUR only",
                bank_account_id=bank.bank_account_id,
                method=self.method,
                currency=payout.currency,
            )

        submitted_at = self._clock.now()
        value_date = submitted_at.date()
        if submitted_at.time() >= self._settings.payout_cutoff_sepa_utc:
            value_date = self._calendar.next_business_day(
                value_date, payout.currency, self.method
            )
        arrival = self._calendar.next_business_day(
            value_date + timedelta(days=1), payout.currency, self.method
        )

        # The message id is what treasury reconciles the pain.002 acknowledgement
        # against, so it has to be stable per payout and unique per file.
        rail_reference = f"PZN{payout.id[-18:].upper()}"

        # TODO(mhandover): wire the real SFTP drop once treasury signs off. Until then
        # the pain.001 is not generated and this returns the identity only; the payout
        # is marked paid manually from the bank statement.
        logger.info(
            "sepa_payout_initiated",
            payout_id=payout.id,
            rail_reference=rail_reference,
            bic=bank.bic,
        )
        return InitiationResult(
            rail_reference=rail_reference,
            submitted_at=submitted_at,
        )
