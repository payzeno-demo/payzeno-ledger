"""Hourly settlement-batch close.

:class:`~app.workers.settlement_import.SettlementImportJob` closes the batch it imports.
This job exists for the batches it did not: a file that arrived in two parts, a batch the
Java service pushed through ``POST /internal/v1/settlement-imports`` and never closed, and
the ones an operator re-opened from the runbook and forgot about.

Closing is what makes a batch reconcilable — :class:`ReconciliationSweepJob` only looks at
``closed`` and ``partially_reconciled`` batches — so an ``open`` batch nobody closes is a
day of merchant settlement that silently never happens. That failure mode is invisible:
there is no error, no alarm and no missing row, just a balance that stops moving.

The rule is age, not completeness. A batch stays open for as long as the importer might
append to it, and after that it is finished whether or not the file matched cleanly:
unmatched lines become orphaned items and get worked by hand, which is a much better
outcome than a batch that waits forever for a line that is never coming.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final

from app.config import Settings
from app.errors import BatchNotReconcilableError, PayzenoLedgerError
from app.logging import get_logger
from app.metrics import metrics
from app.ports import Clock, SessionFactory
from app.repositories.settlement_batch import SettlementBatchRepository
from app.services.settlements import SettlementService
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

INTERVAL_SECONDS: Final[int] = 3600

#: How long a batch may stay open after its processing date before we close it anyway.
#: Six hours covers a split file and a late corrective push; beyond that the acquirer is
#: not sending anything more.
MAX_OPEN_HOURS: Final[int] = 6

#: Batches per pass. There are two acquirers and one batch per currency per day, so this
#: is orders of magnitude above steady state and only matters after a backfill.
BATCH_SIZE: Final[int] = 50


class BatchCloseJob(PeriodicJob):
    """Close settlement batches that have been open longer than :data:`MAX_OPEN_HOURS`."""

    name: ClassVar[str] = "batch_close"

    def __init__(
        self,
        sessions: SessionFactory,
        batches: SettlementBatchRepository,
        settlements: SettlementService,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._batches = batches
        self._settlements = settlements
        self._clock = clock
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS

    async def run_once(self) -> JobResult:
        """Find stale open batches and close each in its own transaction.

        Per-batch transactions because ``close_batch`` publishes ``settlement.batch_closed``
        through the outbox — one transaction over all of them would mean one bad batch
        rolling back the events for the good ones.
        """
        started = time.monotonic()
        now = self._clock.now()
        cutoff = (now - timedelta(hours=MAX_OPEN_HOURS)).date()

        async with self._sessions.begin() as session:
            open_batches = await self._batches.list_by_status(session, ("open",))
            stale_ids = [
                batch.id
                for batch in open_batches[:BATCH_SIZE]
                if batch.processing_date <= cutoff
            ]

        closed = 0
        for batch_id in stale_ids:
            try:
                async with self._sessions.begin() as session:
                    batch = await self._settlements.close_batch(session, batch_id)
            except BatchNotReconcilableError:
                # Something closed it between our read and our write — the importer
                # finishing, or an operator in the runbook. Not a problem.
                logger.debug("batch_already_closed", batch_id=batch_id)
                continue
            except PayzenoLedgerError as exc:
                logger.error(
                    "batch_close_failed", batch_id=batch_id, code=exc.code
                )
                metrics.increment("BatchCloseFailed", code=exc.code)
                continue

            closed += 1
            logger.warning(
                "stale_batch_closed",
                batch_id=batch_id,
                acquirer=batch.acquirer,
                processing_date=batch.processing_date.isoformat(),
                item_count=batch.item_count,
                open_hours_at_least=MAX_OPEN_HOURS,
            )
            metrics.increment("StaleBatchClosed", acquirer=batch.acquirer)

        logger.info(
            "batch_close_pass",
            candidates=len(stale_ids),
            closed=closed,
            cutoff=cutoff.isoformat(),
        )
        return self._result(started, closed)
