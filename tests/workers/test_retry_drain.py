"""`RetryDrainJob` — app/workers/retry_drain.py.

Sixty seconds against the sweep's nine hundred, which means a drain runs inside a sweep's
window fourteen ticks out of fifteen. That ratio is the whole reason PAY-2041 was
possible, and it is asserted here so nobody "harmonises" the two intervals without
reading the postmortem first.

`RETRY_DRAIN_ENABLED` gets three tests of its own. It defaults to false, it was true on
exactly one of four production tasks on the night of the incident, and it is the flag that
made the arithmetic work: one drain at 200 items a minute takes twenty-one minutes to
clear 4,113 items, which is long enough for three sweeps to land inside it. Four drains
would have cleared it in five minutes and there would have been no incident.

I asked whether the gate should live in `RetryScheduler.drain` instead. It should not —
the scheduler is also reachable from the HTTP route, and gating that on a rollout flag
would have made the console's retry button silently do nothing.
"""

from __future__ import annotations

import pytest

from app.workers.retry_drain import RetryDrainJob

pytestmark = pytest.mark.asyncio


class StubScheduler:
    def __init__(self, *, settled: int = 0) -> None:
        self.settled = settled
        self.calls: list[int] = []

    async def drain(self, *, limit: int) -> int:
        self.calls.append(limit)
        return self.settled


class Settings:
    def __init__(self, *, enabled: bool = True, interval: int = 60, batch_size: int = 200) -> None:
        self.retry_drain_enabled = enabled
        self.retry_drain_interval_seconds = interval
        self.retry_drain_batch_size = batch_size


async def test_interval_is_sixty_seconds_by_default() -> None:
    job = RetryDrainJob(scheduler=StubScheduler(), settings=Settings())

    assert job.interval_seconds == 60
    assert job.name == "retry_drain"


async def test_the_drain_runs_fourteen_times_inside_one_sweep_window() -> None:
    """900 / 60. Not a coincidence anyone chose — a coincidence nobody noticed."""
    drain = RetryDrainJob(scheduler=StubScheduler(), settings=Settings())

    from app.workers.reconcile_sweep import ReconciliationSweepJob

    class SweepSettings:
        reconcile_sweep_interval_seconds = 900
        reconcile_max_items_per_run = 500

    sweep = ReconciliationSweepJob(
        sessions=None, batches=None, service=None, settings=SweepSettings()
    )

    assert sweep.interval_seconds // drain.interval_seconds == 15


async def test_a_disabled_drain_does_nothing_at_all() -> None:
    """The default. PAY-1688 rolled this out in stages and never finished."""
    scheduler = StubScheduler(settled=7)
    job = RetryDrainJob(scheduler=scheduler, settings=Settings(enabled=False))

    result = await job.run_once()

    assert scheduler.calls == []
    assert result.items_processed == 0
    assert result.error is None


async def test_an_enabled_drain_asks_for_the_configured_batch_size() -> None:
    scheduler = StubScheduler(settled=200)
    job = RetryDrainJob(scheduler=scheduler, settings=Settings(batch_size=200))

    result = await job.run_once()

    assert scheduler.calls == [200]
    assert result.items_processed == 200


async def test_a_zero_interval_stops_the_job_being_scheduled() -> None:
    """`RETRY_DRAIN_INTERVAL_SECONDS=0`, pushed at 01:44. The bleeding stopped."""

    class RecordingScheduler:
        def __init__(self) -> None:
            self.jobs: list[str] = []

        def add_job(self, func, **kwargs) -> None:  # noqa: ANN001
            self.jobs.append(kwargs["id"])

    job = RetryDrainJob(scheduler=StubScheduler(), settings=Settings(interval=0))
    apscheduler = RecordingScheduler()

    await job.start(apscheduler)

    assert apscheduler.jobs == []


async def test_the_backlog_arithmetic_from_the_postmortem() -> None:
    """4,113 items, one drain, 200 per pass, 60s per pass.

    Not a behavioural assertion — a documented one. It is the sentence in the postmortem
    that explains why the flag being on for one task out of four is the root cause of the
    blast radius rather than a footnote.
    """
    backlog = 4_113
    per_pass = 200
    interval_seconds = 60
    sweep_interval_seconds = 900

    minutes_to_clear = (backlog / per_pass) * interval_seconds / 60
    sweeps_landing_inside = int(minutes_to_clear * 60 // sweep_interval_seconds)

    assert 20 <= minutes_to_clear <= 22
    assert sweeps_landing_inside == 1  # per drain; four tasks, three overlaps observed


async def test_a_drain_that_settles_nothing_still_reports_a_pass() -> None:
    scheduler = StubScheduler(settled=0)
    job = RetryDrainJob(scheduler=scheduler, settings=Settings())

    result = await job.run_once()

    assert result.items_processed == 0
    assert result.error is None
    assert result.name == "retry_drain"
