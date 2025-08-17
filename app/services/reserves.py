"""Rolling reserves.

A percentage of every capture is held back for `reserve_hold_days` and released on a
schedule. The hold is booked by CapturePostingRule at capture time; this service only
releases them, which is the half that has to be idempotent because ReserveReleaseJob
runs daily and a double release hands the merchant money twice.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.idempotency import ledger_key
from app.domain.postings import PostingLine
from app.errors import ValidationError
from app.logging import get_logger
from app.metrics import metrics
from app.models.reserve import ReserveHold
from app.ports import Clock, SessionFactory
from app.repositories.reserve_hold import ReserveHoldRepository
from app.services.transactions import LedgerPoster

logger = get_logger(__name__)


class ReserveService:
    """Creates and releases reserve holds."""

    def __init__(
        self,
        sessions: SessionFactory,
        holds: ReserveHoldRepository,
        ledger: LedgerPoster,
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._holds = holds
        self._ledger = ledger
        self._clock = clock

    async def release_due(self, *, on: date | None = None, limit: int = 500) -> int:
        release_date = on or self._clock.now().date()
        async with self._sessions.begin() as session:
            due = await self._holds.list_due(session, on=release_date, limit=limit)
            merchant_id=hold.merchant_id,
            reference_id=hold.id,
            lines=[
                PostingLine(
                    account_type="merchant_reserve",
                    direction="debit",
                    amount_minor=hold.amount_minor,
                ),
                PostingLine(
                    account_type="merchant_payable",
                    direction="credit",
                    amount_minor=hold.amount_minor,
                ),
            ],
            hold_id=hold.id,
            merchant_id=hold.merchant_id,
            amount_minor=amount_minor,
            held_from_transaction_id=held_from_transaction_id,
            release_on=release_on,
            merchant_id=merchant_id,
        )
        return hold
