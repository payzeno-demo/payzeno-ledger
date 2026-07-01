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
            hold_ids = [hold.id for hold in due]

        released = 0
        for hold_id in hold_ids:
            async with self._sessions.begin() as session:
                if await self._release_one(session, hold_id):
                    released += 1

        logger.info(
            "reserve_release_pass",
            release_date=release_date.isoformat(),
            candidates=len(hold_ids),
            released=released,
        )
        return released

    async def _release_one(self, session: AsyncSession, hold_id: str) -> bool:
        hold = await self._holds.get_or_raise(session, hold_id)
        if hold.released_transaction_id is not None:
            return False

        posted = await self._ledger.post(
            session,
            idempotency_key=ledger_key("reserverelease", hold.merchant_id, hold.id),
            purpose="reserve_release",
            merchant_id=hold.merchant_id,
            currency=hold.currency,
            livemode=hold.livemode,
            reference_type="reserve_hold",
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
            created_by="system",
            request_fingerprint=ledger_key("reservereleasefp", hold.id, hold.release_on.isoformat()),
        )
        hold.released_transaction_id = posted.transaction.id
        hold.updated_at = self._clock.now()
        metrics.increment("ReserveHoldReleased", currency=hold.currency)
        logger.info(
            "reserve_hold_released",
            hold_id=hold.id,
            merchant_id=hold.merchant_id,
            amount_minor=hold.amount_minor,
            transaction_id=posted.transaction.id,
        )
        return True

    async def outstanding_for_merchant(
        self, session: AsyncSession, *, merchant_id: str, currency: str
    ) -> int:
        holds = await self._holds.list_outstanding(
            session, merchant_id=merchant_id, currency=currency
        )
        return sum(hold.amount_minor for hold in holds)

    async def create_hold(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        amount_minor: int,
        held_from_transaction_id: str,
        release_on: date,
        livemode: bool,
    ) -> ReserveHold:
        if amount_minor <= 0:
            raise ValidationError(
                "reserve hold amount must be positive",
                merchant_id=merchant_id,
                amount_minor=amount_minor,
            )
        hold = ReserveHold(
            id=ledger_key("rh", held_from_transaction_id, str(amount_minor))[:26],
            merchant_id=merchant_id,
            currency=currency,
            amount_minor=amount_minor,
            held_from_transaction_id=held_from_transaction_id,
            release_on=release_on,
            livemode=livemode,
        )
        await self._holds.add(session, hold)
        logger.info(
            "reserve_hold_created",
            hold_id=hold.id,
            merchant_id=merchant_id,
            amount_minor=amount_minor,
            release_on=release_on.isoformat(),
        )
        return hold
