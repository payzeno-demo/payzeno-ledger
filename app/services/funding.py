"""Funding — matching money that actually arrived to the batch that claimed it.

The acquirer file says what we are owed. The bank statement says what turned up. Until
those two agree, the ledger has not touched ``cash``: nothing else in this service
debits it. Without that discipline a short-paying acquirer leaves Payzeno paying
merchants out of money that never arrived, and the ledger stays internally balanced the
whole time.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.idempotency import ledger_key
from app.domain.postings import PostingLine
from app.errors import BatchNotFoundError, ValidationError
from app.logging import get_logger
from app.metrics import metrics
from app.models.funding_event import FundingEvent
from app.models.settlement_batch import SettlementBatch
from app.ports import Clock, EventPublisher, SessionFactory
from app.repositories.funding_event import FundingEventRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.services.transactions import LedgerPoster

logger = get_logger(__name__)


class FundingMatchService:
    """Matches unmatched ``funding_event`` rows to reconciled batches."""

    def __init__(
        self,
        sessions: SessionFactory,
        fundings: FundingEventRepository,
        batches: SettlementBatchRepository,
        ledger: LedgerPoster,
        publisher: EventPublisher,
        clock: Clock,
        tolerance_bps: int,
    ) -> None:
        self._sessions = sessions
        self._fundings = fundings
        self._batches = batches
        self._ledger = ledger
        self._publisher = publisher
        self._clock = clock
        self._tolerance_bps = tolerance_bps

    async def match_pending(self, *, limit: int = 200) -> int:
        """Walk unmatched funding events and try to place each one.

        Returns the number matched. Each event gets its own transaction so one
        unplaceable statement line does not block the rest of the morning's feed.
        """
        async with self._sessions.begin() as session:
            events = await self._fundings.list_unmatched(session, limit=limit)
            event_ids = [event.id for event in events]

        matched = 0
        for event_id in event_ids:
            async with self._sessions.begin() as session:
                if await self._match_one(session, event_id):
                    matched += 1

        logger.info("funding_match_pass", candidates=len(event_ids), matched=matched)
        return matched

    async def _match_one(self, session: AsyncSession, event_id: str) -> bool:
        event = await self._fundings.get_or_raise(session, event_id)
        if event.status != "unmatched":
            return False

        candidates = await self._batches.list_unfunded(
            session,
            acquirer=event.acquirer,
            currency=event.currency,
            on_or_before=event.value_date,
        )
        batch = _closest_by_amount(candidates, event.amount_minor)
        if batch is None:
            logger.info(
                "funding_event_unplaced",
                funding_event_id=event.id,
                acquirer=event.acquirer,
                amount_minor=event.amount_minor,
            )
            return False

        variance = event.amount_minor - batch.expected_total_minor
        tolerance = abs(batch.expected_total_minor) * self._tolerance_bps // 10_000
        if abs(variance) > tolerance:
            event.status = "short_paid" if variance < 0 else "disputed"
            event.variance_minor = variance
            event.matched_batch_id = batch.id
            logger.warning(
                "funding_variance_exceeded",
                funding_event_id=event.id,
                batch_id=batch.id,
                variance_minor=variance,
                tolerance_minor=tolerance,
            )
            metrics.increment("FundingVarianceExceeded", acquirer=event.acquirer)
            return False

        transaction_id = await self._post_funding(session, batch, event)
        event.status = "matched"
        event.matched_batch_id = batch.id
        event.variance_minor = variance
        batch.status = "funded"
        batch.funded_amount_minor = event.amount_minor
        batch.funding_event_id = event.id
        batch.funded_at = self._clock.now()

        await self._publisher.publish(
            "settlement.funded",
            {
                "batch_id": batch.id,
                "funding_event_id": event.id,
                "acquirer": batch.acquirer,
                "currency": batch.currency,
                "expected_total_minor": batch.expected_total_minor,
                "funded_amount_minor": event.amount_minor,
                "variance_minor": variance,
                "bank_reference": event.bank_reference,
                "value_date": event.value_date.isoformat(),
                "transaction_id": transaction_id,
                "funded_at": batch.funded_at.isoformat(),
            },
            merchant_id=None,
            correlation_id=batch.id,
            session=session,
            livemode=batch.livemode,
        )
        metrics.increment("SettlementBatchFunded", acquirer=batch.acquirer)
        logger.info(
            "settlement_batch_funded",
            batch_id=batch.id,
            funding_event_id=event.id,
            amount_minor=event.amount_minor,
        )
        return True

    async def record_funding(
        self,
        session: AsyncSession,
        *,
        batch_id: str,
        bank_reference: str,
        amount_minor: int,
        value_date: date,
    ) -> SettlementBatch:
        """Operator-driven funding, from ``POST /settlement-batches/{id}/funding``.

        Used when the statement feed missed a credit or the bank reference does not
        parse. Same posting, same event; the only difference is who decided.
        """
        batch = await self._batches.get(session, batch_id)
        if batch is None:
            raise BatchNotFoundError(f"settlement batch {batch_id} not found", batch_id=batch_id)
        if batch.status not in ("reconciled", "partially_reconciled"):
            raise ValidationError(
                f"batch {batch_id} is {batch.status} and cannot be funded",
                batch_id=batch_id,
                status=batch.status,
            )

        event = await self._fundings.find_by_bank_reference(session, bank_reference)
        if event is None:
            event = FundingEvent(
                id=f"fe_{bank_reference[:24]}",
                acquirer=batch.acquirer,
                currency=batch.currency,
                amount_minor=amount_minor,
                value_date=value_date,
                bank_reference=bank_reference,
                status="unmatched",
                livemode=batch.livemode,
                received_at=self._clock.now(),
            )
            await self._fundings.add(session, event)

        transaction_id = await self._post_funding(session, batch, event)
        event.status = "matched"
        event.matched_batch_id = batch.id
        event.variance_minor = amount_minor - batch.expected_total_minor
        batch.status = "funded"
        batch.funded_amount_minor = amount_minor
        batch.funding_event_id = event.id
        batch.funded_at = self._clock.now()

        logger.info(
            "settlement_batch_funded_manually",
            batch_id=batch.id,
            bank_reference=bank_reference,
            transaction_id=transaction_id,
        )
        return batch

    async def _post_funding(
        self, session: AsyncSession, batch: SettlementBatch, event: FundingEvent
    ) -> str:
        posted = await self._ledger.post(
            session,
            idempotency_key=ledger_key("funding", batch.id, event.id),
            purpose="settlement_funding",
            merchant_id=None,
            currency=batch.currency,
            livemode=batch.livemode,
            reference_type="settlement_batch",
            reference_id=batch.id,
            lines=[
                PostingLine(
                    account_type="cash", direction="debit", amount_minor=event.amount_minor
                ),
                PostingLine(
                    account_type="acquirer_receivable",
                    direction="credit",
                    amount_minor=event.amount_minor,
                ),
            ],
            created_by="system",
            request_fingerprint=ledger_key("fundingfp", batch.id, event.bank_reference),
        )
        return posted.transaction.id


def _closest_by_amount(
    candidates: list[SettlementBatch], amount_minor: int
) -> SettlementBatch | None:
    if not candidates:
        return None
    return min(candidates, key=lambda batch: abs(batch.expected_total_minor - amount_minor))
