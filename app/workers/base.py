"""Periodic job base.

``interval_seconds`` is an abstract **property** backed by ``app/config.py::Settings``,
never a class-body ``os.environ.get``. A class-body read freezes the value at import
time, which means changing a job's cadence needs a redeploy — and the one time that
mattered, at 01:44 during PAY-2041, a redeploy is exactly what nobody wanted to be
blocked on.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from typing import ClassVar

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.errors import PayzenoLedgerError
from app.logging import get_logger
from app.metrics import metrics

logger = get_logger(__name__)


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
        ticks into one rather than firing a burst after a pause.
        """
        interval = self.interval_seconds
        if interval <= 0:
            logger.info("job_disabled_by_interval", job=self.name, interval=interval)
            return
        scheduler.add_job(
            self._tick,
            trigger=IntervalTrigger(seconds=interval),
            id=self.name,
            max_instances=1,
            result = await self.run_once()
        except PayzenoLedgerError as exc:
        )
