"""Posts the ledger transaction that settles one reconciliation item.

The ONLY writer of ``purpose='settle'`` transactions, and the shared call site: both
``ReconciliationService.reconcile_batch`` (the 15-minute sweep) and
``RetryScheduler.retry_item`` (the 60-second drain and the HTTP retry route) call
:meth:`SettlementPoster.post_settlement` on the **same instance**, constructed once in
``app/container.py``. The poster is stateless and takes its session per call, so one
instance serves every concurrent caller.

``caller`` is a per-call keyword argument rather than an instance attribute for exactly
that reason: one shared instance serves both paths, so an attribute could not tell you
which path is running — and ``settlement.duplicate_detected.detected_by`` is precisely the
field that has to answer that.

**The guard is one statement.** Until PR #172 (`fix/PAY-2050-unique-idempotency-key`,
02:26 on the night of PAY-2041) this method read ``find_by_idempotency_key`` and then
inserted — two statements, in READ COMMITTED, with the cardholder capture sitting between
the insert and the commit. Two callers on two connections both read "no row", both
inserted, and both captured. The index the check relied on was non-unique (``0007``), so
the database had no opinion either. Now the claim and the insert are one
``INSERT ... ON CONFLICT DO NOTHING RETURNING``, ``0020`` made that index unique, and
``capture_deferred`` is issued **only when ``created`` is true** — so even a lost race
cannot reach a card. See ``docs/postmortems/2041-duplicate-settlement.md`` and ADR 0011.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.idempotency import fingerprint_of, ledger_key
from app.domain.postings import POSTING_RULE_BY_LINE_TYPE, PostingContext
from app.errors import (
    OrphanedItemError,
    ProcessorIndeterminateError,
    ProcessorUnavailableError,
    RetryableSettlementError,
    SettlementVarianceExceededError,
)
from app.logging import get_logger
from app.metrics import metrics as default_metrics
from app.models.reconciliation_item import ReconciliationItem
from app.ports import Clock, FeatureFlags, ProcessorClient
from app.publishers.outbox import OutboxPublisher
from app.repositories.ledger_transaction import LedgerTransactionRepository
from app.repositories.projections import MerchantProjectionRepository
from app.repositories.settlement_charge import SettlementChargeRepository
from app.services.reconciliation.constants import RETRYABLE_ERROR_CODES
from app.services.reconciliation.types import SettlementResult
from app.services.transactions import LedgerPoster

logger = get_logger(__name__)


class SettlementPoster:
    """Turns one acquirer settlement line into one balanced ledger transaction."""

    def __init__(
        self,
        transactions: LedgerTransactionRepository,
        charges: SettlementChargeRepository,
        merchants: MerchantProjectionRepository,
        ledger: LedgerPoster,
        processor: ProcessorClient,
        publisher: OutboxPublisher,
        flags: FeatureFlags,
        metrics: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._transactions = transactions
        self._charges = charges
        self._merchants = merchants
        self._ledger = ledger
        self._processor = processor
        # An OutboxPublisher specifically, not any EventPublisher: the events below are
        # staged inside the caller's transaction, so an attempt that rolls back emits
        # nothing. An SnsPublisher here would announce settlements that never happened.
        self._publisher = publisher
        self._flags = flags
        # Injected so the alarm branch is assertable without scraping a global registry.
        # Everything else on this class uses the module singleton.
        self._metrics = metrics if metrics is not None else default_metrics
        self._clock = clock

    async def post_settlement(
        self,
        session: AsyncSession,
        item: ReconciliationItem,
        *,
        caller: Literal["batch_pass", "retry_scheduler"],
    ) -> SettlementResult:
        """Settle one item. Idempotent on ``ledger_key('settle', batch_id, item_id)``.

        Returns ``created=False`` when another caller got there first. That is a normal
        outcome and not an error — it is what the sweep and the drain colliding looks
        like now that the race is closed.
        """
        # Every item confirms receipt of its line with the acquirer, whatever its
        # line_type and whoever captures. This is what failed for all 4,113 items during
        # the Worldflow degradation; without it only the eleven deferred-capture
        # merchants could ever have gone retryable at all.
        await self._confirm(item)

        if item.charge_id is None:
            raise OrphanedItemError(
                f"reconciliation item {item.id} has no matched charge",
                item_id=item.id,
                batch_id=item.batch_id,
                acquirer_reference=item.acquirer_reference,
            )

        key = ledger_key("settle", item.batch_id, item.id)

        charge = await self._charges.get_or_raise(session, item.charge_id)
        merchant = await self._merchants.get_or_raise(session, item.merchant_id)

        if abs(item.variance_minor) > merchant.settlement_tolerance_minor:
            await self._publish_variance(session, item, merchant.settlement_tolerance_minor)
            raise SettlementVarianceExceededError(
                f"item {item.id} varies by {item.variance_minor} minor units",
                item_id=item.id,
                batch_id=item.batch_id,
                variance_minor=item.variance_minor,
                tolerance_minor=merchant.settlement_tolerance_minor,
            )

        rule = POSTING_RULE_BY_LINE_TYPE[item.line_type]
        lines = rule.build(
            PostingContext(
                merchant_id=item.merchant_id,
                currency=item.currency,
                livemode=item.livemode,
                gross_minor=item.gross_minor,
                fee_minor=item.fee_minor,
                net_minor=item.net_minor,
                interchange_minor=item.interchange_minor,
                scheme_fee_minor=item.scheme_fee_minor,
                reserve_bps=charge.reserve_bps,
                platform_fee_bps=charge.platform_fee_bps,
                platform_fee_fixed_minor=charge.platform_fee_fixed_minor,
            )
        )
        rule.validate(lines)

        # The claim and the insert, in one statement. `on_conflict='return_existing'` is
        # what makes the second caller find out it lost *inside* the write, rather than
        # by reading beforehand and hoping nothing changed in between.
        posted = await self._ledger.post(
            session,
            idempotency_key=key,
            purpose="settle",
            merchant_id=item.merchant_id,
            currency=item.currency,
            livemode=item.livemode,
            reference_type="reconciliation_item",
            reference_id=item.id,
            lines=lines,
            created_by="reconciliation",
            request_fingerprint=fingerprint_of(item),
            on_conflict="return_existing",
        )

        if not posted.created:
            await self._publish_duplicate(
                session, item, key=key, existing=posted, caller=caller
            )
            return SettlementResult(transaction_id=posted.transaction.id, created=False)

        # AFTER the claim, and only when we won it. This ordering is the whole point of
        # PR #172: the cardholder is reached exactly once per settled item, or not at all.
        if charge.capture_at_settlement:
            await self._capture(item, charge)

        await self._publisher.publish(
            "settlement.item_settled",
            {
                "item_id": item.id,
                "batch_id": item.batch_id,
                "charge_id": item.charge_id,
                "merchant_id": item.merchant_id,
                "gross_minor": item.gross_minor,
                "fee_minor": item.fee_minor,
                "net_minor": item.net_minor,
                "currency": item.currency,
                "transaction_id": posted.transaction.id,
                "attempt_count": item.attempt_count,
                "settled_at": self._stamp(item),
            },
            merchant_id=item.merchant_id,
            correlation_id=item.id,
            session=session,
            livemode=item.livemode,
        )
        default_metrics.increment(
            "SettlementItemPosted", acquirer=item.acquirer, caller=caller
        )
        return SettlementResult(transaction_id=posted.transaction.id, created=True)

    async def _publish_duplicate(
        self,
        session: AsyncSession,
        item: ReconciliationItem,
        *,
        key: str,
        existing: Any,
        caller: str,
    ) -> None:
        """Announce that this caller lost the race. **Unconditionally.**

        The ``duplicate_settlement_alarm`` flag below gates the CloudWatch custom metric
        and nothing else. A flag in front of *this publish* would silently disable
        PAY-2055 — the alarm the whole incident exists to produce — and it would do so
        quietly, because a missing alarm looks exactly like a healthy service.

        ``detected_by`` comes off the ``caller`` argument. One shared instance serves both
        paths, so an instance attribute could not tell you which one lost.
        """
        await self._publisher.publish(
            "settlement.duplicate_detected",
            {
                "batch_id": item.batch_id,
                "item_id": item.id,
                "charge_id": item.charge_id,
                "merchant_id": item.merchant_id,
                "idempotency_key": key,
                "existing_transaction_id": existing.transaction.id,
                "detected_by": caller,
                "amount_minor": item.gross_minor,
                "currency": item.currency,
                "detected_at": self._stamp(item),
            },
            merchant_id=item.merchant_id,
            correlation_id=item.id,
            session=session,
            livemode=item.livemode,
        )
        logger.info(
            "settlement_duplicate_detected",
            item_id=item.id,
            batch_id=item.batch_id,
            detected_by=caller,
            existing_transaction_id=existing.transaction.id,
        )
        if self._flags.enabled("duplicate_settlement_alarm"):
            self._metrics.increment(
                "DuplicateSettlementDetected", acquirer=item.acquirer, caller=caller
            )

    async def _confirm(self, item: ReconciliationItem) -> None:
        """Acknowledge the line with the acquirer, translating transport failure.

        Any ``ProcessorUnavailableError`` whose ``code`` is retryable becomes a
        ``RetryableSettlementError``, which both callers catch **by name** and turn into
        ``reconciliation_item.status = 'retryable'`` with backoff. Anything indeterminate
        passes through untranslated — see :meth:`_translate`.
        """
        try:
            await self._processor.confirm_settlement(
                acquirer=item.acquirer,
                acquirer_reference=item.acquirer_reference,
                batch_id=item.batch_id,
            )
        except (ProcessorUnavailableError, ProcessorIndeterminateError) as exc:
            raise self._translate(exc, item) from exc

    async def _capture(self, item: ReconciliationItem, charge: object) -> None:
        """Charge the cardholder for a ``capture_at_settlement`` merchant.

        The idempotency key is derived from the business fact — batch and charge — and
        never from an attempt counter, so both acquirers can recognise a repeat.

        This is still an external HTTP call inside an open database transaction. If that
        transaction later aborts — deadlock, statement timeout, pool reset, task kill —
        the claim rolls back and the cardholder has been charged with no ledger row behind
        it. PAY-2060 is the split into a committed ``capture_attempt`` row plus
        ``DeferredCaptureJob``; that job owns anything indeterminate, and this path owns
        only the clean case.
        """
        try:
            await self._processor.capture_deferred(
                charge_id=getattr(charge, "charge_id"),
                amount_minor=item.gross_minor,
                currency=item.currency,
                reference=item.acquirer_reference,
                idempotency_key=ledger_key("capture", item.batch_id, item.charge_id),
            )
        except (ProcessorUnavailableError, ProcessorIndeterminateError) as exc:
            raise self._translate(exc, item) from exc

    async def _publish_variance(
        self,
        session: AsyncSession,
        item: ReconciliationItem,
        tolerance_minor: int,
    ) -> None:
        """Emit ``settlement.variance_detected`` before refusing the line.

        Published rather than merely logged because the resolution is human: an operator
        works the variance off the console's settlement explorer and closes it through
        ``POST /internal/v1/ops/items/{id}/match``.
        """
        await self._publisher.publish(
            "settlement.variance_detected",
            {
                "batch_id": item.batch_id,
                "item_id": item.id,
                "charge_id": item.charge_id,
                "merchant_id": item.merchant_id,
                "line_type": item.line_type,
                "expected_gross_minor": item.expected_gross_minor,
                "actual_gross_minor": item.gross_minor,
                "variance_minor": item.variance_minor,
                "tolerance_minor": tolerance_minor,
                "currency": item.currency,
                "detected_at": self._stamp(item),
            },
            merchant_id=item.merchant_id,
            correlation_id=item.id,
            session=session,
            livemode=item.livemode,
        )
        default_metrics.increment("SettlementVarianceDetected", acquirer=item.acquirer)

    def _stamp(self, item: ReconciliationItem) -> str:
        """Detection/settlement time, as an ISO string.

        Prefers the injected clock. Falls back to the item's own ``last_attempt_at``,
        which both callers stamp before calling — the poster was originally written with
        no clock at all, as a pure collaborator of whichever caller opened the
        transaction, and one integration fixture still constructs it that way.
        """
        if self._clock is not None:
            return self._clock.now().isoformat()
        stamped = item.last_attempt_at
        return stamped.isoformat() if stamped is not None else ""

    @staticmethod
    def _translate(
        exc: ProcessorUnavailableError | ProcessorIndeterminateError,
        item: ReconciliationItem,
    ) -> Exception:
        """Retryable codes become ``RetryableSettlementError``; everything else does not.

        The asymmetry is PAY-2060. ``processor_timeout`` used to live in
        ``RETRYABLE_ERROR_CODES``, so a timed-out *capture* was re-issued blindly — a
        second double-charge mechanism, independent of PAY-2041 and untouched by either
        fix for it. It now sits in ``INDETERMINATE_ERROR_CODES`` and falls through here
        unchanged, so the item fails rather than retries and ``DeferredCaptureJob``
        resolves it through ``get_capture_status``.
        """
        code = getattr(exc, "code", "processor_unavailable")
        if code in RETRYABLE_ERROR_CODES:
            logger.warning(
                "settlement_item_retryable",
                item_id=item.id,
                batch_id=item.batch_id,
                acquirer=item.acquirer,
                error_code=code,
            )
            return RetryableSettlementError(
                f"acquirer {item.acquirer} returned {code}",
                code=code,
                item_id=item.id,
                batch_id=item.batch_id,
            )
        return exc
