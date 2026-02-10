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
                tolerance_minor=merchant.settlement_tolerance_minor,
            )

        rule = POSTING_RULE_BY_LINE_TYPE[item.line_type]
        lines = rule.build(
            PostingContext(
                livemode=item.livemode,
                gross_minor=item.gross_minor,
                scheme_fee_minor=item.scheme_fee_minor,
                acquirer=item.acquirer,
                code=code,
                item_id=item.id,
            )
        return exc
