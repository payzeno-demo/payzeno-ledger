"""The eleven APScheduler jobs, and the function that registers them.

Every ledger task runs every enabled job. Production runs **four** tasks, so there are
four unsynchronised copies of each job, and every job in this package has to be safe
against three other copies of itself running at the same moment. The two that matter:

* :class:`~app.workers.reconcile_sweep.ReconciliationSweepJob` serialises through the
  **batch advisory lock** taken inside ``ReconciliationService.reconcile_batch``.
* :class:`~app.workers.retry_drain.RetryDrainJob` serialises through a **row-level
  ``SELECT … FOR UPDATE SKIP LOCKED``** taken inside ``RetryScheduler._claim_item``.

Each is correct against other copies of itself. They are the two schedulers described in
``the-incident.md`` §1, they use disjoint mechanisms, and the retry drain is enabled on
exactly one task because ``RETRY_DRAIN_ENABLED`` defaults to false and PAY-1688's staged
rollout was never widened.

``interval_seconds`` is an abstract **property** on :class:`~app.workers.base.PeriodicJob`,
read at registration time off ``Settings`` or off a module constant — never a class-body
``os.environ.get``, which would freeze the value at import and make changing a job's
cadence a redeploy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.logging import get_logger
from app.workers.base import JobResult, PeriodicJob
from app.workers.batch_close import BatchCloseJob
from app.workers.deferred_capture import DeferredCaptureJob
from app.workers.funding_match import FundingMatchJob
from app.workers.ledger_audit import LedgerAuditJob
from app.workers.negative_balance import NegativeBalanceJob
from app.workers.outbox_drain import OutboxDrainJob
from app.workers.payout_scheduler import PayoutSchedulerJob
from app.workers.reconcile_sweep import ReconciliationSweepJob
from app.workers.reserve_release import ReserveReleaseJob
from app.workers.retry_drain import RetryDrainJob
from app.workers.settlement_import import SettlementImportJob

if TYPE_CHECKING:  # pragma: no cover
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = get_logger(__name__)

#: Container attribute name for each job, in registration order. The order is cosmetic to
#: APScheduler and load-bearing to a human reading the startup log: the two settlement
#: schedulers first, then the money movers, then the housekeeping.
JOB_ATTRIBUTES: tuple[str, ...] = (
    "reconciliation_sweep_job",
    "retry_drain_job",
    "settlement_import_job",
    "batch_close_job",
    "funding_match_job",
    "deferred_capture_job",
    "payout_scheduler_job",
    "reserve_release_job",
    "negative_balance_job",
    "ledger_audit_job",
    "outbox_drain_job",
)

__all__ = [
    "JOB_ATTRIBUTES",
    "BatchCloseJob",
    "DeferredCaptureJob",
    "FundingMatchJob",
    "JobResult",
    "LedgerAuditJob",
    "NegativeBalanceJob",
    "OutboxDrainJob",
    "PayoutSchedulerJob",
    "PeriodicJob",
    "ReconciliationSweepJob",
    "ReserveReleaseJob",
    "RetryDrainJob",
    "SettlementImportJob",
    "register_jobs",
]


async def register_jobs(
    scheduler: "AsyncIOScheduler", container: object
) -> list[PeriodicJob]:
    """Register every job the container built, and return the ones that took.

    A job whose ``interval_seconds`` is non-positive is *unscheduled* and never reaches
    APScheduler — that is what ``RETRY_DRAIN_INTERVAL_SECONDS=0`` did at 01:44 on the
    night of PAY-2041, and what ``LEDGER_AUDIT_INTERVAL_SECONDS=0`` does in staging.

    Note this is **not** the same lever as ``RETRY_DRAIN_ENABLED``. Both the sweep and
    the drain are registered in every task; the drain checks its own enable flag inside
    ``run_once``, per tick, so the flag can be flipped without a redeploy. Zeroing the
    interval is the heavier hammer and needs the task to restart.

    Called once from ``app/main.py``'s lifespan, after the container is built and before
    the app starts serving.
    """
    registered: list[PeriodicJob] = []
    for attribute in JOB_ATTRIBUTES:
        job = getattr(container, attribute, None)
        if job is None:
            # A job in the list with no container attribute is a wiring bug, not a
            # config decision. Loud, but not fatal — the other ten should still run.
            logger.error("job_not_wired", attribute=attribute)
            continue

        interval = job.interval_seconds
        if interval <= 0:
            logger.info("job_disabled", job=job.name, attribute=attribute)
            continue

        await job.start(scheduler)
        registered.append(job)

    logger.info(
        "jobs_registered",
        registered=[job.name for job in registered],
        total=len(JOB_ATTRIBUTES),
        skipped=len(JOB_ATTRIBUTES) - len(registered),
    )
    return registered
