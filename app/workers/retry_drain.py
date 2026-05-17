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
