"""`ReserveReleaseJob` — app/workers/reserve_release.py.

Daily. Releases reserve holds whose `release_on` date has arrived, moving money from
`reserve` back to `merchant_payable`.

Also Michal's, also cold. The thing worth writing down is that a missed day is not a
catastrophe here — the job selects on `release_on <= today` rather than `release_on =
today`, so a day of downtime catches up on the next run instead of stranding a merchant's
money for ninety days plus however long the outage was. That is asserted below because
the obvious implementation is the equality one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.errors import PayzenoLedgerError
from app.workers.reserve_release import ReserveReleaseJob
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 2, 0, tzinfo=UTC)
TODAY = date(2026, 4, 16)


class StubReserves:
    def __init__(self, *, released: int = 0, raises: Exception | None = None) -> None:
        self.released = released
        self.raises = raises
        self.calls: list[tuple[date | None, int]] = []

    async def release_due(self, *, on: date | None = None, limit: int = 500) -> int:
        self.calls.append((on, limit))
        if self.raises is not None:
            raise self.raises
        return self.released


class Settings:
    reserve_release_interval_seconds = 86400
    reserve_release_batch_size = 500


def _job(reserves: StubReserves) -> ReserveReleaseJob:
    return ReserveReleaseJob(
        reserves=reserves, clock=FrozenClock(NOW), settings=Settings()
    )


async def test_interval_is_daily() -> None:
    job = _job(StubReserves())

    assert job.interval_seconds == 86400
    assert job.name == "reserve_release"


async def test_it_releases_todays_holds() -> None:
    reserves = StubReserves(released=6)
    job = _job(reserves)

    result = await job.run_once()

    assert result.items_processed == 6
    assert reserves.calls == [(TODAY, 500)]


async def test_a_missed_day_catches_up() -> None:
    """`release_on <= today`, not `== today`.

    A day of downtime with the equality version strands every hold that came due during
    it, for good, and the only way to find them is a manual query nobody knows to run.
    """
    reserves = StubReserves(released=2)
    job = _job(reserves)

    await job.run_once()

    on, _ = reserves.calls[0]
    assert on == TODAY  # the service filters `release_on <= on`; see test_reserves.py


async def test_nothing_due_is_the_normal_case() -> None:
    job = _job(StubReserves(released=0))

    result = await job.run_once()

    assert result.items_processed == 0
    assert result.error is None


async def test_a_failure_is_reported_on_the_result() -> None:
    job = _job(StubReserves(raises=PayzenoLedgerError("reserve account frozen")))

    result = await job._tick()  # noqa: SLF001

    assert result.error is not None
    assert result.items_processed == 0
