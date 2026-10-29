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

**Per currency (RFC platform-v2 §7).** Each acquirer is asked for every supported currency
for every date in the window — ``?date=&currency=`` — and each non-empty answer is its own
batch. A ``404 file_not_ready`` for one currency or one date is "not yet" for that file
only: the walk carries on to the next currency and the next date. Only the acquirer being
down (5xx, timeouts, an open circuit) stops the walk for that acquirer.

**Simulated time (RFC §10).** The window is normally the last :data:`LOOKBACK_DAYS` days, but
it reaches back to the newest batch this acquirer already has when that is further, up to
:data:`MAX_CATCHUP_DAYS`. A clock that jumps forward a week then imports the week rather than
the last three days of it.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import ClassVar, Final

from app.config import Settings
from app.domain.money import SUPPORTED_CURRENCIES
from app.errors import PayzenoLedgerError, SettlementFileNotReadyError, UpstreamError
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

#: The furthest back a catch-up walk goes after the clock has moved forward. Production
#: never needs it — there is a batch every day — and a month is the longest simulated jump
#: the world makes (RFC platform-v2 §10: "a month of renewals ... in minutes").
MAX_CATCHUP_DAYS: Final[int] = 31

#: Every currency Payzeno settles, asked for one at a time. Order is cosmetic.
SETTLEMENT_CURRENCIES: Final[tuple[str, ...]] = tuple(sorted(SUPPORTED_CURRENCIES))


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
        """Walk the window for each acquirer, oldest date first, every currency per date.

        An upstream failure on one acquirer does not stop the other: they are independent
        vendors and Nordpay being down is not a reason to skip Worldflow's file. An
        already-imported file is a no-op that does not even reach the acquirer.
        """
        started = time.monotonic()
        if not self._settings.settlement_import_enabled:
            # Switched off: unscheduled, and a tick asked for by name does nothing either.
            return self._result(started, 0)
        now = self._clock.now()
        today = now.date()
        imported = 0

        for acquirer in ACQUIRERS:
            oldest = await self._oldest_offset(acquirer, today)
            try:
                for offset in range(oldest, 0, -1):
                    processing_date = today - timedelta(days=offset)
                    for currency in SETTLEMENT_CURRENCIES:
                        if await self._import_one(acquirer, processing_date, currency, now):
                            imported += 1
            except UpstreamError as exc:
                # The acquirer is degraded or unreachable. The next tick is in an hour and
                # there is nothing to escalate; Nordpay's walk still runs below.
                logger.info(
                    "settlement_acquirer_unavailable",
                    acquirer=acquirer,
                    code=exc.code,
                )
                metrics.increment("SettlementImportSkipped", acquirer=acquirer, reason=exc.code)

        return self._result(started, imported)

    async def _oldest_offset(self, acquirer: str, today: date) -> int:
        """How many days back this pass starts: the lookback, or the catch-up gap."""
        latest = await self._importer.latest_processing_date(acquirer)
        if latest is None:
            return LOOKBACK_DAYS
        gap = (today - latest).days
        return min(MAX_CATCHUP_DAYS, max(LOOKBACK_DAYS, gap))

    async def _import_one(
        self, acquirer: str, processing_date: date, currency: str, now: datetime
    ) -> bool:
        """Import one (acquirer, date, currency) file. True when it was imported just now.

        ``UpstreamError`` propagates — it means the acquirer, not the file — and everything
        else is logged here so the walk goes on.
        """
        try:
            batch = await self._importer.import_file(acquirer, processing_date, currency)
        except SettlementFileNotReadyError:
            logger.info(
                "settlement_file_not_ready",
                acquirer=acquirer,
                processing_date=processing_date.isoformat(),
                currency=currency,
            )
            metrics.increment(
                "SettlementImportSkipped", acquirer=acquirer, reason="file_not_ready"
            )
            return False
        except UpstreamError:
            raise
        except PayzenoLedgerError as exc:
            # A parse or matching failure on one file. Log it and carry on — a malformed
            # file for one day or currency must not block the next.
            logger.error(
                "settlement_import_failed",
                acquirer=acquirer,
                processing_date=processing_date.isoformat(),
                currency=currency,
                code=exc.code,
            )
            metrics.increment("SettlementImportFailed", acquirer=acquirer, code=exc.code)
            return False

        if batch is None or batch.closed_at is None or batch.closed_at < now:
            # Empty for this currency, or imported on an earlier tick.
            return False
        logger.info(
            "settlement_file_imported",
            acquirer=acquirer,
            processing_date=processing_date.isoformat(),
            currency=currency,
            batch_id=batch.id,
            item_count=batch.item_count,
            expected_total_minor=batch.expected_total_minor,
        )
        metrics.increment("SettlementImported", acquirer=acquirer)
        return True
