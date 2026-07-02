"""Nightly ledger audit — the invariant that everything else assumes.

Three checks, in this order:

1. **Trial balance per currency.** Debits equal credits, or the ledger is not a ledger.
   A failure publishes ``ledger.imbalance_detected`` and raises ``LedgerIntegrityError``
   — a genuine 500-class fault, and the only one in this repository that deserves the
   name.
2. **Duplicate settlements.** Two ``purpose='settle'`` transactions carrying the same
   ``idempotency_key``. Added by PAY-2054 during the arc INC hardening: before migration
   ``0020`` made the index unique this was structurally possible, and the audit is what
   would have caught it on the first night rather than the fifth.
3. **Balance-cache drift.** ``merchant_balance_cache`` versus a live sum over
   ``ledger_entry``. The cache is a denormalisation and a denormalisation nobody checks is
   a rumour. This is the check behind the ``LedgerBalanceCacheDrift`` alarm — the one that
   fired first on the night of PAY-2041 and paged the wrong person, because a duplicate
   settlement moves the cache before anyone notices the duplicate.

Runs at a daily cadence. The checks are read-heavy — the trial balance sums every entry
for a currency — so they are timed for the trough after the settlement import has closed
its batches.
"""

from __future__ import annotations

import time
from typing import ClassVar, Final

from app.config import Settings
from app.errors import LedgerIntegrityError
from app.logging import get_logger
from app.metrics import metrics
from app.services.audit import LedgerAuditService
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

INTERVAL_SECONDS: Final[int] = 86_400

#: The currencies Payzeno settles in. Restated rather than read off `CURRENCIES` because
#: the trial balance is expensive and running it for a currency with no accounts is a
#: full scan that returns two zeroes.
AUDITED_CURRENCIES: Final[tuple[str, ...]] = ("USD", "EUR", "GBP", "ILS")


class LedgerAuditJob(PeriodicJob):
    """Assert the ledger's own invariants and alarm on any that fail."""

    name: ClassVar[str] = "ledger_audit"

    def __init__(self, audit: LedgerAuditService, settings: Settings) -> None:
        self._audit = audit
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        if not self._settings.ledger_audit_enabled:
            return 0
        return INTERVAL_SECONDS

    async def run_once(self) -> JobResult:
        """Run all three checks and report how many findings there were.

        ``LedgerIntegrityError`` is caught rather than propagated: an imbalance in USD
        must not stop the EUR, GBP and ILS checks from running, and the service has
        already published ``ledger.imbalance_detected`` by the time it raises. The base
        class would otherwise turn the first failure into the whole job's result and
        every other invariant would go unchecked on exactly the night one of them broke.
        """
        started = time.monotonic()
        findings = 0

        for currency in AUDITED_CURRENCIES:
            try:
                result = await self._audit.run_trial_balance(currency=currency)
            except LedgerIntegrityError as exc:
                findings += 1
                logger.error(
                    "trial_balance_raised",
                    currency=currency,
                    currency=currency,
                    delta_minor=result.delta_minor,
                    debit_total_minor=result.debit_total_minor,
                )

        duplicates = await self._audit.check_duplicate_settlements()
        if duplicates:
            findings += duplicates
            logger.error("duplicate_settlements_found", count=duplicates)
            metrics.increment("DuplicateSettlementDetected", source="nightly_audit")

        drifted = await self._audit.check_balance_cache_drift()
        if drifted:
            findings += drifted
            logger.error("balance_cache_drift", merchants=drifted)
            metrics.observe("BalanceCacheDrift", drifted)

        logger.info(
            "ledger_audit_pass",
            currencies=len(AUDITED_CURRENCIES),
            findings=findings,
            duplicates=duplicates,
            drifted=drifted,
        )
        return self._result(started, findings)
