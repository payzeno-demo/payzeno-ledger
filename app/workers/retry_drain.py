"""The 60-second retry drain.

Introduced by PAY-1607 alongside :class:`~app.services.reconciliation.retry.RetryScheduler`
so a transient acquirer failure no longer waits up to fifteen minutes for the next sweep.

Two things about this job are load-bearing and neither is obvious:

**It is gated by ``RETRY_DRAIN_ENABLED``, which defaults to false.** That is a leftover
from PAY-1688's staged rollout — the drain was enabled on one ledger task to watch it
before widening, and nobody widened it. Production runs four tasks, so on any given
minute exactly one of them drains and the other three sweep. A 4,000-item backlog
therefore drains at one task's rate while three sweeps keep passing over the same items.

**It shares its ``RetryScheduler`` with the HTTP route.** ``app/api/routers/reconciliation.py``
resolves the same instance out of the container, which holds the same
:class:`~app.services.reconciliation.poster.SettlementPoster` the 900s sweep uses. One
settlement implementation, three callers.

The drain itself is serial: :meth:`RetryScheduler.drain` walks its candidate list one item
at a time. Firing ``limit`` retries concurrently would undo the backoff column entirely
and hammer an acquirer that is already returning 504s.
"""

from __future__ import annotations

import time
from typing import ClassVar

from app.config import Settings
from app.logging import get_logger
from app.metrics import metrics
from app.services.reconciliation.retry import RetryScheduler
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

class RetryDrainJob(PeriodicJob):
    """Drain the retryable reconciliation backlog, oldest ``next_attempt_at`` first."""

    name: ClassVar[str] = "retry_drain"

    def __init__(self, scheduler: RetryScheduler, settings: Settings) -> None:
        self._scheduler = scheduler
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        """``RETRY_DRAIN_INTERVAL_SECONDS``, 60.

        Read on every registration rather than captured at import, so the interval can
        be changed with a task restart instead of a redeploy. During PAY-2041 that
        distinction is what let the 01:44 mitigation happen at all: setting it to 0
        stops the job being scheduled without touching a line of code.
        """
        return self._settings.retry_drain_interval_seconds

    async def run_once(self) -> JobResult:
        """One drain pass, capped at ``RETRY_DRAIN_BATCH_SIZE`` items.

        The job is registered in every task regardless of ``RETRY_DRAIN_ENABLED`` — the
        flag is checked *here*, per tick, so flipping it takes effect on the next minute
        instead of on the next deploy. That is the whole reason it is a setting and not a
        registration-time decision.

        The cap is a wall-clock budget in disguise: each item makes at least one acquirer
        call, so 200 items at 300ms is a minute, which is exactly the interval. Setting
        it higher does not drain faster — ``max_instances=1`` on the scheduler means the
        next tick is skipped while this one is still running — it only makes the pass
        take longer to notice a shutdown.
        """
        started = time.monotonic()

        if not self._settings.retry_drain_enabled:
            return JobResult(
                name=self.name, items_processed=0, duration_ms=0, error=None
            )

        limit = self._settings.retry_drain_batch_size
        settled = await self._scheduler.drain(limit=limit)

        metrics.increment("RetryDrainPass", settled=str(settled > 0).lower())
        metrics.observe("RetryDrainSettled", settled)
        logger.info(
