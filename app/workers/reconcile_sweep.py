"""The 900-second batch sweep.

Registered in every ledger task. Production runs four tasks, so there are four
unsynchronised sweeps, and the batch advisory lock inside
``ReconciliationService.reconcile_batch`` is what keeps them from colliding with each
other.
"""

from __future__ import annotations

import time
from typing import ClassVar

from app.config import Settings
from app.errors import PayzenoLedgerError
from app.logging import get_logger
from app.ports import SessionFactory
from app.repositories.settlement_batch import SettlementBatchRepository
from app.services.reconciliation.constants import RECONCILABLE_BATCH_STATUSES
from app.services.reconciliation.reconciler import ReconciliationService
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)


class ReconciliationSweepJob(PeriodicJob):
    """Reconciles every batch that is closed but not yet fully settled."""

    name: ClassVar[str] = "reconciliation_sweep"

    def __init__(
        self,
        sessions: SessionFactory,
        batches: SettlementBatchRepository,
        service: ReconciliationService,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._batches = batches
        self._service = service
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        return self._settings.reconcile_sweep_interval_seconds

    async def run_once(self) -> JobResult:
        started = time.monotonic()

        async with self._sessions.begin() as session:
            batches = await self._batches.list_by_status(
                session, tuple(sorted(RECONCILABLE_BATCH_STATUSES))
            )
            batch_ids = [batch.id for batch in batches]

