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
            currency=currency,
            debit_minor=result.debit_minor,
            credit_minor=result.credit_minor,
        )
        return result

    async def check_duplicate_settlements(self, *, since: datetime | None = None) -> int:
        """Invariant (1): exactly one ``settle`` transaction per settled charge.

        This is the check that would have caught PAY-2041 on the first sweep. It did not
        exist then, because every check that did exist was a *balance* check and a
        duplicate settlement balances perfectly.
        """
        window_start = since or (self._clock.now() - DUPLICATE_LOOKBACK)
        async with self._sessions.begin() as session:
            duplicates = await self._transactions.list_duplicate_idempotency_keys(
                session, purpose="settle", since=window_start
            )
            if duplicates:
                await self._publish_imbalance(
                    session,
                    check="batch_total",
                    currency=duplicates[0].currency,
                    expected=len(duplicates),
                    actual=sum(row.count for row in duplicates),
                    samples=[row.sample_transaction_id for row in duplicates[:10]],
                )

        if duplicates:
            metrics.increment("DuplicateSettlementDetected", source="audit")
            logger.error(
                "duplicate_settlements_found",
                key_count=len(duplicates),
                since=window_start.isoformat(),
            )
        return len(duplicates)

    async def check_balance_cache_drift(self, *, limit: int = 500) -> int:
        """Invariant (3): the cache equals the sum of the entries behind it."""
        drifted = 0
        async with self._sessions.begin() as session:
            rows = await self._balances.list_stale(session, limit=limit)
            for row in rows:
                recomputed = await self._entries.sum_by_account_and_purpose(
                    session,
                    merchant_id=row.merchant_id,
                    currency=row.currency,
                    livemode=row.livemode,
                    as_of=self._clock.now(),
                )
                expected = recomputed.get("merchant_payable", 0)
                if expected != row.available_minor:
                    drifted += 1
                    logger.error(
                        "balance_cache_drift",
                        merchant_id=row.merchant_id,
                        currency=row.currency,
                        cached_minor=row.available_minor,
                        recomputed_minor=expected,
                    )
                    await self._publish_imbalance(
                        session,
                        check="merchant_balance",
                        currency=row.currency,
                        expected=expected,
                        actual=row.available_minor,
                        samples=[],
                        merchant_id=row.merchant_id,
                    )
        if drifted:
            metrics.increment("LedgerImbalanceDetected", check="merchant_balance")
        return drifted

    async def _publish_imbalance(
        self,
        session: AsyncSession,
        *,
        check: str,
        currency: str,
        expected: int,
        actual: int,
        samples: list[str],
        merchant_id: str | None = None,
    ) -> None:
        await self._publisher.publish(
            "ledger.imbalance_detected",
            {
                "check": check,
                "merchant_id": merchant_id,
                "currency": currency,
                "expected_minor": expected,
                "actual_minor": actual,
                "delta_minor": actual - expected,
                "sample_transaction_ids": samples,
                "detected_at": self._clock.now().isoformat(),
            },
            merchant_id=merchant_id,
            correlation_id=f"audit:{check}:{currency}",
            session=session,
        )


class AdjustmentService:
    """Maker-checker for manual ledger adjustments.

    ``AdjustmentPostingRule`` is reachable only through an approved request. A human
    posting arbitrary entries against merchant money with no approval record is the
    first thing an auditor asks about, and this database has no ``audit_log`` table to
    fall back on.
    """

    def __init__(
        self,
        requests: LedgerAdjustmentRequestRepository,
        ledger: LedgerPoster,
        clock: Clock,
    ) -> None:
        self._requests = requests
        self._ledger = ledger
        self._clock = clock

    async def request(
        self,
        session: AsyncSession,
        *,
        merchant_id: str | None,
        currency: str,
        lines: list[dict[str, object]],
        reason_code: str,
        requested_by: str,
    ) -> LedgerAdjustmentRequest:
        if not lines:
            raise ValidationError("an adjustment needs at least one line", reason_code=reason_code)
        if not reason_code:
            raise ValidationError("reason_code is required", merchant_id=merchant_id)

        record = LedgerAdjustmentRequest(
            id=ledger_key("lar", requested_by, reason_code)[:26],
            merchant_id=merchant_id,
            currency=currency,
            lines=lines,
            reason_code=reason_code,
            requested_by=requested_by,
            requested_at=self._clock.now(),
            status="pending",
            livemode=True,
        )
        await self._requests.add(session, record)
        logger.info(
            "adjustment_requested",
            request_id=record.id,
            merchant_id=merchant_id,
            reason_code=reason_code,
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
            idempotency_key=ledger_key("adjustment", record.id, record.reason_code),
            purpose="adjustment",
            merchant_id=record.merchant_id,
            currency=record.currency,
            livemode=record.livemode,
            reference_type="ledger_adjustment_request",
            reference_id=record.id,
            lines=posting_lines,
            created_by="admin",
            request_fingerprint=ledger_key("adjustmentfp", record.id, approved_by),
        )

        record.approved_by = approved_by
        record.approved_at = self._clock.now()
        record.status = "posted"
        record.posted_transaction_id = posted.transaction.id
        logger.info(
            "adjustment_approved",
            request_id=record.id,
            approved_by=approved_by,
            transaction_id=posted.transaction.id,
            note=approver_note[:120],
        )
        return record
