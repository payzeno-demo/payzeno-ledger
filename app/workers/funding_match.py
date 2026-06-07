"""Match unmatched bank credits against settlement batches, every 15 minutes.

Reconciliation says the acquirer *agreed* the numbers. Funding says the money *arrived*.
They are separate facts and conflating them is how a platform pays out against a credit
that never landed.

``funding_event`` rows come in from treasury's bank feed with a reference, an amount and a
value date. This job walks the unmatched ones and asks
:class:`~app.services.funding.FundingMatchService` to bind each to a batch, within
``FUNDING_MATCH_TOLERANCE_BPS`` — acquirers routinely net a few basis points of FX or a
monthly platform fee out of the wire, and demanding an exact match would leave every batch
unfunded and every merchant's ``available`` balance pinned at zero.
"""

from __future__ import annotations

import time
from typing import ClassVar, Final

from app.config import Settings
from app.logging import get_logger
from app.metrics import metrics
from app.services.funding import FundingMatchService
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

#: Fifteen minutes. The bank feed lands in batches through the morning and treasury
#: watches the funded column while it does.
INTERVAL_SECONDS: Final[int] = 900

#: Events per pass. The feed delivers a few hundred a day across both acquirers; the cap
#: exists so a backlog after an outage does not turn one tick into a ten-minute pass.
BATCH_SIZE: Final[int] = 200


class FundingMatchJob(PeriodicJob):
    """Bind unmatched ``funding_event`` rows to the batches they paid for."""

    name: ClassVar[str] = "funding_match"

    def __init__(self, funding: FundingMatchService, settings: Settings) -> None:
        self._funding = funding
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS

    async def run_once(self) -> JobResult:
        """One matching pass.

        ``match_pending`` returns how many events it bound. An unmatched event is not an
        error — it is usually a credit for a batch that has not closed yet, and it will
        be picked up on a later pass. Events that stay unmatched for more than a day are
        what the ``LedgerUnmatchedFunding`` alarm watches, and that alarm reads the table,
        not this job.
        """
        started = time.monotonic()
        matched = await self._funding.match_pending(limit=BATCH_SIZE)

        metrics.observe("FundingEventsMatched", matched)
        if matched:
            logger.info(
                "funding_match_pass",
                matched=matched,
                limit=BATCH_SIZE,
                tolerance_bps=self._settings.funding_match_tolerance_bps,
            )
        else:
            logger.debug("funding_match_pass_empty", limit=BATCH_SIZE)
        return self._result(started, matched)
