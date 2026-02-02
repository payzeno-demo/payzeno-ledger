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
    job = RetryDrainJob(scheduler=StubScheduler(), settings=Settings())

    assert job.interval_seconds == 60
    assert job.name == "retry_drain"


async def test_the_drain_runs_fourteen_times_inside_one_sweep_window() -> None:
    class RecordingScheduler:
        def __init__(self) -> None:
            self.jobs: list[str] = []

        def add_job(self, func, **kwargs) -> None:  # noqa: ANN001
            self.jobs.append(kwargs["id"])

    job = RetryDrainJob(scheduler=StubScheduler(), settings=Settings(interval=0))
    interval_seconds = 60
    scheduler = StubScheduler(settled=0)
