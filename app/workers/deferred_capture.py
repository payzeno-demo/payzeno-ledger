"""Deferred-capture worker — PAY-2060, post-incident hardening.

The second double-charge mechanism, and the one neither the PAY-2043 hotfix nor PR #172
addressed.

Before this job existed, ``SettlementPoster`` called ``capture_deferred`` inline and any
failure went down the ordinary retry path. That is correct for
``processor_unavailable`` — the acquirer refused the call, nothing happened, retrying is
free. It is **wrong** for ``processor_timeout``: a timeout on a capture is precisely the
state where we do not know whether the cardholder was charged, and re-issuing it is a
coin flip on double-charging them. ``processor_timeout`` sat in ``RETRYABLE_ERROR_CODES``
for the whole pre-hardening history and fired on every acquirer degradation.

PAY-2060 moved it into ``INDETERMINATE_ERROR_CODES``, added the ``capture_attempt`` table
(migration ``0025``) and this 30-second job. An indeterminate attempt is now resolved by
asking the acquirer what actually happened — ``ProcessorClient.get_capture_status`` keyed
on the same ``idempotency_key`` the capture used — rather than by guessing.

Thirty seconds is not a throughput number. It is how long a cardholder-visible ambiguity
is allowed to stay unresolved.
"""

from __future__ import annotations

import time
from typing import ClassVar, Final

from app.config import Settings
from app.logging import get_logger
from app.metrics import metrics
from app.services.captures import DeferredCaptureService
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

INTERVAL_SECONDS: Final[int] = 30

#: Attempts per pass. Only the eleven ``capture_at_settlement`` merchants generate these,
#: so a hundred is several hours of normal volume and enough to clear a whole degradation
#: in a handful of passes.
BATCH_SIZE: Final[int] = 100


class DeferredCaptureJob(PeriodicJob):
    """Issue pending captures and resolve indeterminate ones."""

    name: ClassVar[str] = "deferred_capture"

    def __init__(self, captures: DeferredCaptureService, settings: Settings) -> None:
        self._captures = captures
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS

    async def run_once(self) -> JobResult:
        """One pass over ``capture_attempt`` rows that are not yet terminal.

        ``process_pending`` handles both states behind ``pix_capture_attempt_pending``:
        a ``pending`` attempt is issued, an ``indeterminate`` one is resolved against
        ``get_capture_status`` and only re-issued if the acquirer says it never captured.
        The service owns that branch because the same logic is reachable from the
        settlement path directly.
        """
        started = time.monotonic()
        processed = await self._captures.process_pending(limit=BATCH_SIZE)

        metrics.observe("DeferredCapturesProcessed", processed)
        if processed:
            logger.info(
                "deferred_capture_pass", processed=processed, limit=BATCH_SIZE
            )
        return self._result(started, processed)
