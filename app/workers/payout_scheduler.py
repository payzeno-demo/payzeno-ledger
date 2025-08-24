"""Hourly payout state advance.

A payout created by ``PayoutService.create_payout`` is ``scheduled`` with an
``available_on`` date computed off the per-rail cutoff and the banking calendar. This job
is what moves it to ``in_transit`` once that date arrives — the point after which
cancellation is no longer possible because the instruction has gone to the rail.

Hourly, not daily, because the four rails have four cutoffs in four jurisdictions:
``PAYOUT_CUTOFF_ACH_UTC``, ``PAYOUT_CUTOFF_SAME_DAY_ACH_UTC``, ``PAYOUT_CUTOFF_SEPA_UTC``,
``PAYOUT_CUTOFF_FASTER_PAYMENTS_UTC``. A single daily tick would advance a SEPA payout
eleven hours after its cutoff and a same-day ACH payout after its window had closed.

Owned by ``mhandover`` until the handover; untouched since apart from keeping it
compiling. The ``in_transit`` transition is written against the ``Payout`` row directly
rather than through a service method, which is the one place in this repository a worker
mutates an aggregate without going through its service. It predates ``PayoutService``
having a method worth calling and nobody has been back.
"""

from __future__ import annotations

import time
from typing import ClassVar, Final

from app.config import Settings
from app.logging import get_logger
from app.metrics import metrics
from app.ports import Clock, SessionFactory
from app.services.payouts import PayoutService
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

INTERVAL_SECONDS: Final[int] = 3600

#: Statuses this job will advance. Anything else — paid, failed, canceled, returned — is
#: terminal or owned by treasury's reconciliation, and touching it here would race with
#: ``PayoutService.mark_paid``.
ADVANCEABLE: Final[frozenset[str]] = frozenset({"scheduled"})


class PayoutSchedulerJob(PeriodicJob):
    """Advance due payouts from ``scheduled`` to ``in_transit``."""

    name: ClassVar[str] = "payout_scheduler"

    def __init__(
        self,
        sessions: SessionFactory,
        payouts: PayoutService,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._payouts = payouts
        self._clock = clock
        self._settings = settings

    async def run_once(self) -> JobResult:
        """Advance every payout whose ``available_on`` has arrived.

        One session for the whole pass. The volume is small — a few hundred payouts a day
        platform-wide — and holding one transaction over all of them means the state
        advance is atomic with respect to a concurrent ``mark_paid``, which takes a row
        lock on the payout it is confirming.
        """
        started = time.monotonic()
        today = self._clock.now().date()
        advanced = 0

        async with self._sessions.begin() as session:
            due = await self._payouts.due_payouts(session, on=today)
            for payout in due:
                if payout.status not in ADVANCEABLE:
                    continue
                payout.status = "in_transit"
                advanced += 1
                logger.info(
                    "payout_in_transit",
                    payout_id=payout.id,
                    merchant_id=payout.merchant_id,
                    method=payout.method,
                    amount_minor=payout.amount_minor,
                    available_on=payout.available_on.isoformat(),
                )

        metrics.observe("PayoutsAdvanced", advanced)
        if advanced:
            metrics.increment("PayoutSchedulerPass", advanced=str(advanced))
        logger.info(
            "payout_scheduler_pass",
            due=len(due),
            advanced=advanced,
            on=today.isoformat(),
            ach_cutoff=self._settings.payout_cutoff_ach_utc,
        )
        return self._result(started, advanced)
