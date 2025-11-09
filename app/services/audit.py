"""Nightly integrity checks and dual-controlled manual adjustments.

Two classes, one file, because they are the same concern from two directions: one
detects that the ledger has stopped telling the truth, the other is the only sanctioned
way a human is allowed to change it.

A note the postmortem quotes: a **duplicate settlement is internally balanced**. Every
check in :class:`LedgerAuditService` that predates PAY-2041 passes on a double-posted
batch, which is why invariant (1) — exactly one ``settle`` transaction per settled
charge — exists at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.idempotency import ledger_key
from app.domain.postings import PostingLine
from app.errors import (
    DualControlRequiredError,
    LedgerIntegrityError,
    NotFoundError,
    ValidationError,
)
from app.logging import get_logger
from app.metrics import metrics
from app.models.ledger_adjustment_request import LedgerAdjustmentRequest
from app.ports import Clock, EventPublisher, SessionFactory
from app.repositories.balance_cache import MerchantBalanceCacheRepository
from app.repositories.ledger_adjustment import LedgerAdjustmentRequestRepository
from app.repositories.ledger_entry import LedgerEntryRepository
from app.repositories.ledger_transaction import LedgerTransactionRepository
from app.services.transactions import LedgerPoster

logger = get_logger(__name__)

#: How far back the duplicate-settle check looks on a nightly run.
DUPLICATE_LOOKBACK = timedelta(days=2)


@dataclass(slots=True)
class TrialBalanceResult:
    currency: str
    as_of: datetime
    debit_minor: int = 0
    credit_minor: int = 0
    balanced: bool = True
    failures: list[dict[str, object]] = field(default_factory=list)

    @property
    def delta_minor(self) -> int:
        return self.debit_minor - self.credit_minor


class LedgerAuditService:
    """Runs the cross-cutting invariants that no single write path can enforce."""

    def __init__(
        self,
        sessions: SessionFactory,
        entries: LedgerEntryRepository,
        transactions: LedgerTransactionRepository,
        balances: MerchantBalanceCacheRepository,
        publisher: EventPublisher,
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._entries = entries
        self._transactions = transactions
        self._balances = balances
        self._publisher = publisher
        self._clock = clock

    async def run_trial_balance(
        self, *, currency: str, as_of: datetime | None = None
    ) -> TrialBalanceResult:
        """Invariant (2): total debits equal total credits, per currency."""
        stamp = as_of or self._clock.now()
        result = TrialBalanceResult(currency=currency, as_of=stamp)

        async with self._sessions.begin() as session:
            totals = await self._entries.trial_balance_by_currency(
                session, currency=currency, as_of=stamp
            )
            result.debit_minor = totals.debit_minor
            result.credit_minor = totals.credit_minor
            result.balanced = totals.debit_minor == totals.credit_minor

            if not result.balanced:
                samples = await self._entries.sample_unbalanced_transactions(
                    session, currency=currency, as_of=stamp, limit=10
                )
                result.failures.append(
                    {
                        "check": "trial_balance",
                        "sample_transaction_ids": samples,
                    }
                )
                await self._publish_imbalance(
                    session,
                    check="trial_balance",
                    currency=currency,
                    expected=totals.credit_minor,
                    actual=totals.debit_minor,
                    samples=samples,
                )

        if not result.balanced:
            metrics.increment("LedgerImbalanceDetected", check="trial_balance")
            raise LedgerIntegrityError(
                f"trial balance for {currency} is out by {result.delta_minor} minor units",
                currency=currency,
                delta_minor=result.delta_minor,
            )

        logger.info(
            "trial_balance_ok",
            id=ledger_key("lar", requested_by, reason_code)[:26],
            merchant_id=merchant_id,
            reason_code=reason_code,
            requested_by=requested_by,
            requested_by=requested_by,
        )
        return record

    async def approve(
        self,
        session: AsyncSession,
        request_id: str,
        *,
        approved_by: str,
        approver_note: str,
    ) -> LedgerAdjustmentRequest:
        record = await self._requests.get(session, request_id)
        if record is None:
            raise NotFoundError(
                f"adjustment request {request_id} not found", request_id=request_id
            )
        if record.status != "pending":
            raise ValidationError(
                f"adjustment request {request_id} is {record.status}",
                request_id=request_id,
                status=record.status,
            )
        if approved_by == record.requested_by:
            raise DualControlRequiredError(
                "an adjustment cannot be approved by its requester",
                request_id=request_id,
                requested_by=record.requested_by,
            )

        posting_lines = [
            PostingLine(
                account_type=str(line["account_type"]),
                direction=str(line["direction"]),  # type: ignore[arg-type]
                amount_minor=int(line["amount_minor"]),
            )
            for line in record.lines
        ]
        posted = await self._ledger.post(
            session,
        )
        return record
