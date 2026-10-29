"""`SettlementImportJob` — app/workers/settlement_import.py.

Hourly, and gated by `SETTLEMENT_IMPORT_ENABLED` because during a cutover you sometimes
want the ledger running with no new files arriving.

The job imports for both acquirers on every tick. Worldflow files land around 04:00 UTC
and Nordpay's around 06:30, and rather than model two schedules the job just asks for
yesterday's file every hour and relies on `uq_settlement_batch_file` to make the repeat a
no-op. That is not elegant. It is also the reason a late file gets picked up within the
hour instead of waiting a day, which is worth more than elegance to the people who get
asked why a merchant has not been paid.

Since RFC platform-v2 §7 "asks for yesterday's file" means one file per currency: the
acquirer is asked `?date=&currency=` for each of the five, for each date in the window,
and a `404 file_not_ready` for one of them is "not yet" for that file only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app.errors import (
    ProcessorUnavailableError,
    SettlementFileNotReadyError,
    ValidationError,
)
from app.workers.settlement_import import (
    LOOKBACK_DAYS,
    MAX_CATCHUP_DAYS,
    SETTLEMENT_CURRENCIES,
    SettlementImportJob,
)
from tests.doubles import FrozenClock
from tests.factories import make_batch

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 7, 0, tzinfo=UTC)
YESTERDAY = date(2026, 4, 15)

Key = tuple[str, date, str]


class StubImporter:
    """`SettlementImportService`'s two methods the job calls.

    By default every file is a closed batch imported *this tick* (``closed_at == NOW``).
    ``raises`` and ``returns`` override per acquirer, or per ``(acquirer, date, currency)``.
    """

    def __init__(
        self,
        *,
        raises: dict[Any, Exception] | None = None,
        returns: dict[Any, Any] | None = None,
        latest: dict[str, date] | None = None,
    ) -> None:
        self.calls: list[Key] = []
        self.raises = raises or {}
        self.returns = returns or {}
        self.latest = latest or {}

    async def latest_processing_date(self, acquirer: str) -> date | None:
        return self.latest.get(acquirer)

    async def import_file(self, acquirer: str, processing_date: date, currency: str) -> Any:
        key = (acquirer, processing_date, currency)
        self.calls.append(key)
        for probe in (key, acquirer):
            if probe in self.raises:
                raise self.raises[probe]
            if probe in self.returns:
                return self.returns[probe]
        batch = make_batch(
            batch_id=f"sb_{acquirer}_{processing_date}_{currency}",
            acquirer=acquirer,
            currency=currency,
            status="closed",
            processing_date=processing_date,
        )
        batch.closed_at = NOW
        return batch


class Settings:
    def __init__(self, *, enabled: bool = True) -> None:
        self.settlement_import_enabled = enabled
        self.settlement_import_interval_seconds = 3600


def _job(
    importer: StubImporter, *, enabled: bool = True, now: datetime = NOW
) -> SettlementImportJob:
    return SettlementImportJob(
        importer=importer, clock=FrozenClock(now), settings=Settings(enabled=enabled)
    )


def _calls_for(importer: StubImporter, acquirer: str) -> list[Key]:
    return [call for call in importer.calls if call[0] == acquirer]


async def test_interval_is_hourly() -> None:
    job = _job(StubImporter())

    assert job.interval_seconds == 3600
    assert job.name == "settlement_import"


async def test_it_imports_both_acquirers() -> None:
    """Two acquirers, two file formats, one job. `parser_for` sorts out the difference."""
    importer = StubImporter()
    job = _job(importer)

    await job.run_once()

    assert {call[0] for call in importer.calls} == {"worldflow", "nordpay"}


async def test_it_asks_for_every_currency_for_every_date_in_the_lookback() -> None:
    """Acquirers file yesterday's activity, one file per currency (RFC platform-v2 §7)."""
    importer = StubImporter()
    job = _job(importer)

    await job.run_once()

    expected_dates = {YESTERDAY - timedelta(days=n) for n in range(LOOKBACK_DAYS)}
    for acquirer in ("worldflow", "nordpay"):
        calls = _calls_for(importer, acquirer)
        assert {call[1] for call in calls} == expected_dates
        assert {call[2] for call in calls} == {"USD", "EUR", "GBP", "ILS", "JPY"}
        assert len(calls) == LOOKBACK_DAYS * len(SETTLEMENT_CURRENCIES)
    assert NOW.date() not in {call[1] for call in importer.calls}


async def test_it_walks_oldest_date_first() -> None:
    importer = StubImporter()
    job = _job(importer)

    await job.run_once()

    dates = [call[1] for call in _calls_for(importer, "worldflow")]
    assert dates == sorted(dates)


async def test_items_processed_counts_only_files_imported_this_tick() -> None:
    """An empty currency is `None`; a file closed on an earlier tick is not news."""
    earlier = make_batch(batch_id="sb_old", acquirer="worldflow", status="closed")
    earlier.closed_at = NOW - timedelta(hours=1)
    importer = StubImporter(
        returns={
            "nordpay": None,
            ("worldflow", YESTERDAY, "USD"): earlier,
        }
    )
    job = _job(importer)

    result = await job.run_once()

    # worldflow: 3 dates x 5 currencies, minus the one imported an hour ago.
    assert result.items_processed == LOOKBACK_DAYS * len(SETTLEMENT_CURRENCIES) - 1


async def test_a_disabled_import_does_nothing() -> None:
    importer = StubImporter()
    job = _job(importer, enabled=False)

    result = await job.run_once()

    assert importer.calls == []
    assert result.items_processed == 0


async def test_a_file_not_ready_skips_that_file_only() -> None:
    """`404 file_not_ready` for yesterday's EUR is "not yet" — not "stop looking".

    The walk used to `break` on any upstream error, so one unpublished file hid every
    older date behind it. Now the other currencies of the same day, and the other days,
    are still asked for.
    """
    not_ready = SettlementFileNotReadyError("file_not_ready", acquirer="worldflow")
    importer = StubImporter(raises={("worldflow", YESTERDAY, "EUR"): not_ready})
    job = _job(importer)

    result = await job.run_once()

    assert len(_calls_for(importer, "worldflow")) == LOOKBACK_DAYS * len(SETTLEMENT_CURRENCIES)
    assert result.error is None
    assert result.items_processed == 2 * LOOKBACK_DAYS * len(SETTLEMENT_CURRENCIES) - 1


async def test_one_acquirer_being_down_does_not_block_the_other() -> None:
    """Worldflow was down for twenty-two minutes once. Nordpay was not.

    Down means down: the first unavailable answer ends Worldflow's walk for this tick,
    rather than spending fourteen more requests confirming it.
    """
    importer = StubImporter(
        raises={"worldflow": ProcessorUnavailableError(code="processor_unavailable")}
    )
    job = _job(importer)

    result = await job.run_once()

    assert len(_calls_for(importer, "worldflow")) == 1
    assert len(_calls_for(importer, "nordpay")) == LOOKBACK_DAYS * len(SETTLEMENT_CURRENCIES)
    assert result.items_processed == LOOKBACK_DAYS * len(SETTLEMENT_CURRENCIES)


async def test_a_malformed_file_is_reported_not_swallowed_silently() -> None:
    """A parse failure is a real problem and somebody has to see it.

    It does not raise — that would take the scheduler's job with it — but the pass
    reports fewer imports than files, and that gap is what the dashboard alarms on.
    """
    importer = StubImporter(raises={"nordpay": ValidationError("short record", row=41)})
    job = _job(importer)

    result = await job.run_once()

    assert len(_calls_for(importer, "nordpay")) == LOOKBACK_DAYS * len(SETTLEMENT_CURRENCIES)
    assert result.items_processed == LOOKBACK_DAYS * len(SETTLEMENT_CURRENCIES)
    assert result.error is None


async def test_after_a_clock_jump_it_catches_up_from_the_last_batch() -> None:
    """Simulated time (RFC §10) moves days at once. A week's jump imports the week."""
    importer = StubImporter(latest={"worldflow": NOW.date() - timedelta(days=8)})
    job = _job(importer)

    await job.run_once()

    worldflow_dates = {call[1] for call in _calls_for(importer, "worldflow")}
    nordpay_dates = {call[1] for call in _calls_for(importer, "nordpay")}
    assert min(worldflow_dates) == NOW.date() - timedelta(days=8)
    assert max(worldflow_dates) == YESTERDAY
    assert len(worldflow_dates) == 8
    assert len(nordpay_dates) == LOOKBACK_DAYS


async def test_the_catch_up_is_bounded() -> None:
    importer = StubImporter(latest={"worldflow": NOW.date() - timedelta(days=400)})
    job = _job(importer)

    await job.run_once()

    assert len({call[1] for call in _calls_for(importer, "worldflow")}) == MAX_CATCHUP_DAYS
