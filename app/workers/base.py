"""Periodic job base.

``interval_seconds`` is an abstract **property** backed by ``app/config.py::Settings``,
never a class-body ``os.environ.get``. A class-body read freezes the value at import
time, which means changing a job's cadence needs a redeploy — and the one time that
mattered, at 01:44 during PAY-2041, a redeploy is exactly what nobody wanted to be
blocked on.

**Time can jump.** Locally the whole stack runs on simulated time (RFC platform-v2 §10) and
the world advances the clock by days at a time. APScheduler's defaults would then *skip*
every overdue tick — a run more than ``misfire_grace_time`` (1s) late is dropped as missed —
and an hourly job would sit idle until its next slot, a daily one for up to a day. So every
job registers with ``misfire_grace_time=None`` (a late run is still a run) and
``coalesce=True`` (however many slots were missed, it runs once), on a
:class:`CatchUpIntervalTrigger` that resumes from *now* instead of stepping through every
missed slot one interval at a time. ``POST /internal/v1/ops/jobs/{name}/run`` calls
:meth:`PeriodicJob.run_now` for the jobs whose next slot is too far off to wait for.
"""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.errors import PayzenoLedgerError
from app.logging import get_logger
from app.metrics import metrics

logger = get_logger(__name__)


class CatchUpIntervalTrigger(IntervalTrigger):  # type: ignore[misc]
    """An ``IntervalTrigger`` that, once behind by more than an interval, resumes from now.

    APScheduler works out a job's missed run times by stepping from the last fire time one
    interval at a time. After a thirty-day jump that is 518,400 steps for the 5-second
    outbox drain, on the event loop, before anything runs. Restarting the arithmetic from
    the trigger's start date lands on the next slot after ``now`` in one step instead, and
    with ``coalesce=True`` the overdue run still fires exactly once.
    """

    def get_next_fire_time(self, previous_fire_time: datetime | None, now: datetime) -> Any:
        if previous_fire_time is not None and now - previous_fire_time > self.interval:
            previous_fire_time = None
        return super().get_next_fire_time(previous_fire_time, now)


@dataclass(frozen=True, slots=True)
class JobResult:
    name: str
    items_processed: int
    duration_ms: int
    error: str | None


class PeriodicJob(abc.ABC):
    """One scheduled unit of background work."""

    name: ClassVar[str]

    @property
    @abc.abstractmethod
    def interval_seconds(self) -> int:
        """Cadence in seconds, read from ``Settings`` on every scheduler tick."""

    @abc.abstractmethod
    async def run_once(self) -> JobResult:
        """Do one pass. Must not raise — failures come back on ``JobResult.error``."""

    async def start(self, scheduler: AsyncIOScheduler) -> None:
        """Register with APScheduler.

        ``max_instances=1`` matters: a pass that overruns its interval must not have a
        second copy of itself started behind it. ``coalesce=True`` collapses missed
        ticks into one rather than firing a burst after a pause, and
        ``misfire_grace_time=None`` makes that one run happen however late it is — see the
        module docstring for why a clock jump needs both.
        """
        interval = self.interval_seconds
        if interval <= 0:
            logger.info("job_disabled_by_interval", job=self.name, interval=interval)
            return
        scheduler.add_job(
            self._tick,
            trigger=CatchUpIntervalTrigger(seconds=interval),
            id=self.name,
            name=self.name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
            replace_existing=True,
        )
        logger.info("job_registered", job=self.name, interval_seconds=interval)

    async def run_now(self) -> JobResult:
        """One tick, now, on request — ``POST /internal/v1/ops/jobs/{name}/run``.

        The same wrapper the scheduler calls, so it never raises either. A scheduled tick
        already in progress in this process is waited for rather than overlapped.
        """
        logger.info("job_run_requested", job=self.name)
        return await self._tick()

    def _run_lock(self) -> asyncio.Lock:
        # Lazily created: concretes do not call super().__init__(), and the lock has to
        # be made on the loop that uses it.
        lock: asyncio.Lock | None = getattr(self, "_tick_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._tick_lock = lock
        return lock

    async def _tick(self) -> JobResult:
        """Wrap :meth:`run_once` so no job can take the scheduler down with it.

        Serialised per job within the process, so an on-demand :meth:`run_now` and the
        scheduler's own tick take turns. Copies in other tasks are each job's own problem
        and always were (``app/workers/__init__.py``).
        """
        async with self._run_lock():
            return await self._tick_unlocked()

    async def _tick_unlocked(self) -> JobResult:
        started = time.monotonic()
        try:
            result = await self.run_once()
        except PayzenoLedgerError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.error(
                "job_failed", job=self.name, code=exc.code, duration_ms=duration_ms
            )
            metrics.increment("JobFailed", job=self.name, code=exc.code)
            return JobResult(
                name=self.name, items_processed=0, duration_ms=duration_ms, error=exc.code
            )
        except Exception as exc:  # noqa: BLE001 - a job must never kill the scheduler
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.error(
                "job_crashed", job=self.name, error=str(exc), duration_ms=duration_ms
            )
            metrics.increment("JobFailed", job=self.name, code="unhandled")
            return JobResult(
                name=self.name,
                items_processed=0,
                duration_ms=duration_ms,
                error=str(exc)[:200],
            )

        metrics.observe("JobDurationMs", result.duration_ms, job=self.name)
        metrics.increment("JobCompleted", job=self.name)
        return result

    def _result(self, started: float, items_processed: int) -> JobResult:
        """Convenience for subclasses: build a successful result from a start marker."""
        return JobResult(
            name=self.name,
            items_processed=items_processed,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=None,
        )
