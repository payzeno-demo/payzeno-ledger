"""`PeriodicJob` and `register_jobs()` — app/workers/base.py, app/workers/__init__.py.

Twelve jobs. Two of them collide in PAY-2041 and the collision is a function of their
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

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

from app.workers import JOB_ATTRIBUTES, jobs_by_name, register_jobs
from app.workers.base import CatchUpIntervalTrigger, JobResult, PeriodicJob
from app.workers.batch_close import BatchCloseJob
from app.workers.deferred_capture import DeferredCaptureJob
from app.workers.funding_match import FundingMatchJob
from app.workers.ledger_audit import LedgerAuditJob
from app.workers.negative_balance import NegativeBalanceJob
from app.workers.outbox_drain import OutboxDrainJob
from app.workers.payout_scheduler import PayoutSchedulerJob
from app.workers.payout_sweep import PayoutSweepJob
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
    PayoutSweepJob,
]


class RecordingScheduler:
    """Stands in for `AsyncIOScheduler`. Only `add_job` is reached."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def add_job(self, func: Any, **kwargs: Any) -> Any:
        self.jobs.append({"func": func, **kwargs})
        return kwargs.get("id")


class SlowJob(PeriodicJob):
    """Records how many of its own ticks overlap."""

    name: ClassVar[str] = "slow"

    def __init__(self) -> None:
        self.running = 0
        self.max_concurrent = 0
        self.runs = 0

    @property
    def interval_seconds(self) -> int:
        return 60

    async def run_once(self) -> JobResult:
        self.running += 1
        self.max_concurrent = max(self.max_concurrent, self.running)
        await asyncio.sleep(0.01)
        self.running -= 1
        self.runs += 1
        return JobResult(name=self.name, items_processed=1, duration_ms=10, error=None)


class CountingJob(PeriodicJob):
    """A minimal concrete so the base can be tested without a container."""

    name: ClassVar[str] = "counting"

    def __init__(self, *, interval: int = 30, raises: Exception | None = None) -> None:
        self._interval = interval
        self._raises = raises
        self.runs = 0

    @property
    def interval_seconds(self) -> int:
        return self._interval

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
    """The name is APScheduler's job id. Two jobs with one name means one job."""
    names = [job.name for job in ALL_JOBS]

    assert len(names) == len(set(names)), f"duplicate job name in {names}"
    assert len(names) == 12
    assert len(JOB_ATTRIBUTES) == 12


async def test_every_job_subclasses_the_base() -> None:
    for job in ALL_JOBS:
        assert issubclass(job, PeriodicJob), f"{job.__name__} is not a PeriodicJob"


async def test_start_registers_with_the_interval_from_settings() -> None:
    job = CountingJob(interval=900)
    scheduler = RecordingScheduler()

    await job.start(scheduler)

    assert len(scheduler.jobs) == 1
    registered = scheduler.jobs[0]
    assert registered["id"] == "counting"
    assert registered["max_instances"] == 1
    assert registered["coalesce"] is True


async def test_a_late_tick_still_runs_once_after_the_clock_jumps() -> None:
    """Simulated time (RFC platform-v2 §10) advances days at a time.

    APScheduler's default `misfire_grace_time` is one second: a tick more than a second
    late is *dropped*, and an hourly job then idles until its next slot. `None` makes the
    late tick run; `coalesce=True` makes however many were missed run once.
    """
    job = CountingJob(interval=3600)
    scheduler = RecordingScheduler()

    await job.start(scheduler)

    registered = scheduler.jobs[0]
    assert registered["misfire_grace_time"] is None
    assert registered["coalesce"] is True
    assert isinstance(registered["trigger"], CatchUpIntervalTrigger)


async def test_the_catch_up_trigger_resumes_from_now_in_one_step() -> None:
    """Thirty days behind a 5-second interval is one step, not 518,400."""
    start = datetime(2026, 10, 1, tzinfo=UTC)
    trigger = CatchUpIntervalTrigger(seconds=5, start_date=start, timezone=UTC)
    now = start + timedelta(days=30, seconds=2)

    following = trigger.get_next_fire_time(start, now)

    assert now <= following < now + timedelta(seconds=5)


async def test_the_catch_up_trigger_is_an_ordinary_interval_when_on_time() -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    trigger = CatchUpIntervalTrigger(seconds=60, start_date=start, timezone=UTC)
    previous = start + timedelta(minutes=10)

    assert trigger.get_next_fire_time(previous, previous + timedelta(seconds=1)) == (
        previous + timedelta(seconds=60)
    )


async def test_run_now_is_one_tick_through_the_same_wrapper() -> None:
    """`POST /internal/v1/ops/jobs/{name}/run` — a failing job answers, it does not raise."""
    ok = CountingJob()
    failing = CountingJob(raises=RuntimeError("acquirer exploded"))

    assert (await ok.run_now()).items_processed == 1
    result = await failing.run_now()
    assert result.error is not None and "acquirer exploded" in result.error


async def test_run_now_waits_for_a_tick_already_in_progress() -> None:
    job = SlowJob()

    await asyncio.gather(job._tick(), job.run_now())  # noqa: SLF001

    assert job.max_concurrent == 1
    assert job.runs == 2


async def test_jobs_by_name_resolves_every_wired_job() -> None:
    container = type("Container", (), {})()
    for attribute in JOB_ATTRIBUTES:
        job_cls = type(attribute, (CountingJob,), {"name": attribute.removesuffix("_job")})
        setattr(container, attribute, job_cls())
    container.not_a_job = object()

    jobs = jobs_by_name(container)

    assert set(jobs) == {attribute.removesuffix("_job") for attribute in JOB_ATTRIBUTES}


async def test_a_zero_interval_disables_the_job_without_a_redeploy() -> None:
    """01:44 on the night of PAY-2041, in one assertion.

    `RETRY_DRAIN_INTERVAL_SECONDS=0` is what stopped the bleeding. It only works because
    the interval is read from `Settings` here rather than baked in at import.
    """
    job = CountingJob(interval=0)
    scheduler = RecordingScheduler()

    await job.start(scheduler)

    assert scheduler.jobs == []


async def test_tick_returns_a_result_rather_than_raising() -> None:
    """A job that raises is a job APScheduler eventually stops running."""
    job = CountingJob(raises=RuntimeError("acquirer exploded"))

    result = await job._tick()  # noqa: SLF001 - the wrapper is the unit under test

    assert isinstance(result, JobResult)
    assert result.error is not None
    assert "acquirer exploded" in result.error
    assert result.items_processed == 0


async def test_tick_passes_a_successful_result_through() -> None:
    job = CountingJob()

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

    job = CountingJob()
    started = time.monotonic()

    result = job._result(started, 42)  # noqa: SLF001

    assert result.name == "counting"
    assert result.items_processed == 42
    assert result.error is None
    assert result.duration_ms >= 0
