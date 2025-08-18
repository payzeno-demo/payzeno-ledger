"""Nightly reserve release.

A reserve hold takes a percentage of a merchant's settlement — ``charge.reserve_bps`` — and
parks it in the ``merchant_reserve`` account against future chargebacks. Each hold carries
a ``release_on`` date, typically settlement + 90 days for a standard-risk merchant and
settlement + 180 for an elevated one.

This job posts the release: ``merchant_reserve`` debit, ``merchant_payable`` credit, one
transaction per hold. From the merchant's point of view money they earned three months ago
becomes payable overnight, which is why it runs at a fixed daily cadence rather than
opportunistically — a merchant's finance team plans against it.

Owned by ``mhandover``. Cold since the handover.
"""

from __future__ import annotations

import time
from typing import ClassVar, Final

from app.config import Settings
from app.logging import get_logger
from app.metrics import metrics
from app.ports import Clock
from app.services.reserves import ReserveService
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

#: Daily. The release date is a date, not a timestamp — releasing at 04:00 or 22:00 makes
#: no difference to anyone as long as it happens once.
INTERVAL_SECONDS: Final[int] = 86_400

#: Holds per pass. Ninety days of holds coming due on one date is bounded by the volume
#: ninety days ago, and 500 has never been hit outside a backfill.
BATCH_SIZE: Final[int] = 500


class ReserveReleaseJob(PeriodicJob):
    """Release every reserve hold whose ``release_on`` has arrived."""

    name: ClassVar[str] = "reserve_release"

    def __init__(
        self, reserves: ReserveService, clock: Clock, settings: Settings
    ) -> None:
        self._reserves = reserves
        self._clock = clock
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS

    async def run_once(self) -> JobResult:
        """One release pass over ``pix_reserve_hold_due``.

        ``ReserveService.release_due`` takes each hold in its own transaction: a single
        merchant with a frozen account must not roll back four hundred other merchants'
        releases. The partial index means the scan only ever sees holds that are
        outstanding and due, so the pass is cheap even against a table with years of
        released rows in it.
        """
        started = time.monotonic()
        today = self._clock.now().date()
        released = await self._reserves.release_due(on=today, limit=BATCH_SIZE)

        metrics.observe("ReserveHoldsReleased", released)
        logger.info(
            "reserve_release_pass",
            released=released,
            on=today.isoformat(),
            limit=BATCH_SIZE,
        )
        if released == BATCH_SIZE:
            # We hit the cap, so there are more due today than we released. The next tick
            # is 24h away, which is too long — this is worth knowing about.
            logger.warning(
                "reserve_release_capped",
                limit=BATCH_SIZE,
                on=today.isoformat(),
            )
            metrics.increment("ReserveReleaseCapped")
        return self._result(started, released)
