"""Hourly acquirer settlement-file import.

Pulls yesterday's file from each acquirer, parses it, opens a batch, matches items
against the ``settlement_charge`` projection and closes the batch. The closed batch is
what makes :class:`~app.workers.reconcile_sweep.ReconciliationSweepJob` pick it up.

Runs hourly rather than daily on purpose: Worldflow publishes its file some time between
02:00 and 06:00 UTC depending on load, Nordpay publishes at 04:30 and re-publishes a
corrected file perhaps once a month. Polling hourly and relying on
``uq_settlement_batch_file`` for idempotency is simpler than either service telling us
when they are ready, and the re-import of a corrected file lands on the existing batch.

The Java service's push at ``POST /internal/v1/settlement-imports`` is a *different*
path with a different service method (``SettlementService.import_legacy_records``). The
two duplicate about forty lines of matching. See ``docs/adr/0007-strangle-billing-legacy.md``.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final

from app.config import Settings
from app.errors import PayzenoLedgerError, UpstreamError
from app.logging import get_logger
from app.metrics import metrics
from app.ports import Clock
from app.services.settlements import SettlementImportService
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

#: Not an env var: the cadence has never needed to differ per environment, and adding
#: SETTLEMENT_IMPORT_INTERVAL_SECONDS would be a config knob with one reader and no
#: caller who has ever wanted to turn it. `SETTLEMENT_IMPORT_ENABLED` is the switch that
#: does get used — staging points at the sandbox acquirer and imports nothing real.
INTERVAL_SECONDS: Final[int] = 3600

#: Both acquirers, always in this order. Worldflow is roughly 80% of volume, so importing
#: it first means the sweep has the bulk of the day's work available an hour earlier on a
#: morning when Nordpay is late.
ACQUIRERS: Final[tuple[str, ...]] = ("worldflow", "nordpay")

#: How far back to look. A file that has not appeared within three days is an incident,
#: not something a poller fixes, and re-walking a longer window every hour costs an
#: acquirer round trip per day per acquirer for nothing.
LOOKBACK_DAYS: Final[int] = 3


class SettlementImportJob(PeriodicJob):
    """Fetch, parse and open a settlement batch per acquirer per processing date."""

    name: ClassVar[str] = "settlement_import"

    def __init__(
        self,
        importer: SettlementImportService,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._importer = importer
        self._clock = clock
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        if not self._settings.settlement_import_enabled:
            return 0
        return INTERVAL_SECONDS

    async def run_once(self) -> JobResult:
        """Walk the lookback window for each acquirer.

        An upstream failure on one acquirer does not stop the other: they are independent
        vendors and Nordpay being down is not a reason to skip Worldflow's file. An
        already-imported date is a no-op — ``open_batch`` upserts on the file reference.
        """
        started = time.monotonic()
        today = self._clock.now().date()
        imported = 0

        for acquirer in ACQUIRERS:
            for offset in range(1, LOOKBACK_DAYS + 1):
                processing_date = today - timedelta(days=offset)
                try:
                    batch = await self._importer.import_file(acquirer, processing_date)
                except UpstreamError as exc:
                    # The file is not there yet, or the acquirer is degraded. Either way
                    # the next tick is in an hour and there is nothing to escalate.
                    logger.info(
                        "settlement_file_unavailable",
                        acquirer=acquirer,
                        processing_date=processing_date.isoformat(),
                        code=exc.code,
                    )
                    metrics.increment(
                        "SettlementImportSkipped", acquirer=acquirer, reason=exc.code
                    )
                    break
                except PayzenoLedgerError as exc:
                    # A parse or matching failure on one date. Log it and carry on to the
                    # next — a malformed file for one day must not block the day after.
                    logger.error(
                        "settlement_import_failed",
                        acquirer=acquirer,
                        processing_date=processing_date.isoformat(),
                        code=exc.code,
                    )
                    metrics.increment(
                        "SettlementImportFailed", acquirer=acquirer, code=exc.code
                    )
                    continue

                imported += 1
                logger.info(
                    "settlement_file_imported",
                    acquirer=acquirer,
                    processing_date=processing_date.isoformat(),
                    batch_id=batch.id,
                    item_count=batch.item_count,
                    gross_minor=batch.gross_minor,
                )
                metrics.increment("SettlementImported", acquirer=acquirer)

        return self._result(started, imported)
