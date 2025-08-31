"""`SettlementImportJob` — app/workers/settlement_import.py.

Hourly, and gated by `SETTLEMENT_IMPORT_ENABLED` because during a cutover you sometimes
want the ledger running with no new files arriving.

The job imports for both acquirers on every tick. Worldflow files land around 04:00 UTC
and Nordpay's around 06:30, and rather than model two schedules the job just asks for
yesterday's file every hour and relies on `uq_settlement_batch_file` to make the repeat a
no-op. That is not elegant. It is also the reason a late file gets picked up within the
hour instead of waiting a day, which is worth more than elegance to the people who get
asked why a merchant has not been paid.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.errors import ProcessorUnavailableError, ValidationError
from app.workers.settlement_import import SettlementImportJob
from tests.doubles import FrozenClock
from tests.factories import make_batch

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 7, 0, tzinfo=UTC)
YESTERDAY = date(2026, 4, 15)


class StubImporter:
    def __init__(self, *, raises: dict[str, Exception] | None = None) -> None:
        self.calls: list[tuple[str, date]] = []
        self.raises = raises or {}

    async def import_file(self, acquirer: str, processing_date: date) -> Any:
        self.calls.append((acquirer, processing_date))
        if acquirer in self.raises:
            raise self.raises[acquirer]
        return make_batch(
            batch_id=f"sb_{acquirer}", acquirer=acquirer, status="closed",
            processing_date=processing_date,
        )


class Settings:
    def __init__(self, *, enabled: bool = True) -> None:
        self.settlement_import_enabled = enabled
        self.settlement_import_interval_seconds = 3600


def _job(importer: StubImporter, *, enabled: bool = True) -> SettlementImportJob:
    return SettlementImportJob(
        importer=importer, clock=FrozenClock(NOW), settings=Settings(enabled=enabled)
    )


async def test_interval_is_hourly() -> None:
    job = _job(StubImporter())

    assert job.interval_seconds == 3600
    assert job.name == "settlement_import"


async def test_it_imports_both_acquirers() -> None:
    """Two acquirers, two file formats, one job. `parser_for` sorts out the difference."""
    importer = StubImporter()
    job = _job(importer)

    result = await job.run_once()

    assert {call[0] for call in importer.calls} == {"worldflow", "nordpay"}
    assert result.items_processed == 2


async def test_it_asks_for_the_previous_processing_date() -> None:
    """Acquirers file yesterday's activity. Asking for today's returns a 404 all day."""
    importer = StubImporter()
    job = _job(importer)

    await job.run_once()

    assert {call[1] for call in importer.calls} == {YESTERDAY}


async def test_a_disabled_import_does_nothing() -> None:
    importer = StubImporter()
    job = _job(importer, enabled=False)

    result = await job.run_once()

    assert importer.calls == []
    assert result.items_processed == 0


async def test_one_acquirer_being_down_does_not_block_the_other() -> None:
    """Worldflow was down for twenty-two minutes once. Nordpay was not."""
    importer = StubImporter(
        raises={"worldflow": ProcessorUnavailableError(code="processor_unavailable")}
    )
    job = _job(importer)

    result = await job.run_once()

    assert [call[0] for call in importer.calls] == ["worldflow", "nordpay"]
    assert result.items_processed == 1


async def test_a_malformed_file_is_reported_not_swallowed_silently() -> None:
    """A parse failure is a real problem and somebody has to see it.

    It does not raise — that would take the scheduler's job with it — but the pass
    reports fewer imports than acquirers, and that gap is what the dashboard alarms on.
    """
    importer = StubImporter(raises={"nordpay": ValidationError("short record", row=41)})
    job = _job(importer)

    result = await job.run_once()

    assert result.items_processed == 1
    assert result.error is None
