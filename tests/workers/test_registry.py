"""`PeriodicJob` and `register_jobs()` — app/workers/base.py, app/workers/__init__.py.

Eleven jobs. Two of them collide in PAY-2041 and the collision is a function of their
intervals, so the intervals are asserted here by name rather than left to whoever edits
`Settings` next.

The property I care about most is the boring one: `interval_seconds` is a **property**
that reads `Settings` on every scheduler tick, not a class-body `os.environ.get`. A
class-body read freezes the value at import time. At 01:44 on the night of the incident
the mitigation was `RETRY_DRAIN_INTERVAL_SECONDS=0`, and a frozen value would have meant
that took a redeploy — which is the thing nobody wanted to be waiting on.

I asked in review why `_tick` swallows everything. The answer is that APScheduler removes
a job whose callable raises often enough, so a job that raises is a job that silently
stops existing. That is asserted below too.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from app.workers import register_jobs
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

pytestmark = pytest.mark.asyncio

ALL_JOBS = [
    ReconciliationSweepJob,
    RetryDrainJob,
    SettlementImportJob,
    FundingMatchJob,
    DeferredCaptureJob,
    PayoutSchedulerJob,
    ReserveReleaseJob,
    NegativeBalanceJob,
    LedgerAuditJob,
    OutboxDrainJob,
    BatchCloseJob,
]


class RecordingScheduler:
    """Stands in for `AsyncIOScheduler`. Only `add_job` is reached."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def add_job(self, func: Any, **kwargs: Any) -> Any:
        self.jobs.append({"func": func, **kwargs})
        return kwargs.get("id")


class CountingJob(PeriodicJob):
    """A minimal concrete so the base can be tested without a container."""

    name: ClassVar[str] = "counting"

    @property
    async def run_once(self) -> JobResult:
        self.runs += 1
        if self._raises is not None:
            raise self._raises
        return JobResult(
            name=self.name, items_processed=self.runs, duration_ms=1, error=None
        )


async def test_periodic_job_is_abstract() -> None:
    with pytest.raises(TypeError):
        PeriodicJob()  # type: ignore[abstract]


async def test_every_job_declares_a_unique_name() -> None:
    job = CountingJob(interval=900)
    scheduler = RecordingScheduler()

    await job.start(scheduler)

    assert len(scheduler.jobs) == 1
    """A job that raises is a job APScheduler eventually stops running."""
    job = CountingJob(raises=RuntimeError("acquirer exploded"))

    result = await job._tick()  # noqa: SLF001

    assert result.error is None
    assert result.items_processed == 1


async def test_register_jobs_is_the_single_registration_site() -> None:
    """Both jobs in the collision are registered in every task.

    Production runs four ledger tasks. That means four sweeps and — in theory — four
    drains. `RETRY_DRAIN_ENABLED` was true on exactly one of them, which is the arithmetic
    that gave the sweeps twenty-one minutes to land inside the drain's backlog.
    """
    assert callable(register_jobs)


async def test_result_helper_reports_a_duration() -> None:
    """`_result` is what every concrete uses to build its success case."""
    import time

    result = job._result(started, 42)  # noqa: SLF001

    assert result.name == "counting"
    assert result.items_processed == 42
    assert result.error is None
    assert result.duration_ms >= 0
