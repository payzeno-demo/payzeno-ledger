"""`FundingMatchJob` — app/workers/funding_match.py.

Every fifteen minutes, matches bank credits against settlement batches. Until a batch is
funded its `merchant_payable` credits do not count toward `compute_available`, so a
merchant does not get paid until this job runs. It is quiet, and then it is the most
urgent job in the service.

Matching is by amount within a tolerance (`FUNDING_MATCH_TOLERANCE_BPS`) because the bank
takes a wire fee out of the credit and the acquirer's number and the bank's number are
therefore never identical. Five basis points on a $2M settlement is $1,000, which sounds
generous until you notice the alternative is a human matching them in a spreadsheet.
"""

from __future__ import annotations

import pytest

from app.errors import PayzenoLedgerError
from app.workers.funding_match import FundingMatchJob

pytestmark = pytest.mark.asyncio


class StubService:
    def __init__(self, *, matched: int = 0, raises: Exception | None = None) -> None:
        self.matched = matched
        self.raises = raises
        self.calls: list[int] = []

    async def match_pending(self, *, limit: int) -> int:
        self.calls.append(limit)
        if self.raises is not None:
            raise self.raises
        return self.matched


class Settings:
    funding_match_interval_seconds = 900
    funding_match_batch_size = 200
    funding_match_tolerance_bps = 5


async def test_interval_is_fifteen_minutes() -> None:
    job = FundingMatchJob(funding=StubService(), settings=Settings())

    assert job.interval_seconds == 900
    assert job.name == "funding_match"


async def test_it_reports_what_it_matched() -> None:
    service = StubService(matched=4)
    job = FundingMatchJob(funding=service, settings=Settings())

    result = await job.run_once()

    assert result.items_processed == 4
    assert result.error is None
    assert service.calls == [200]


async def test_a_pass_with_nothing_to_match_is_normal() -> None:
    """Most passes match nothing. Banks credit once a day; this runs ninety-six times."""
    job = FundingMatchJob(funding=StubService(matched=0), settings=Settings())

    result = await job.run_once()

    assert result.items_processed == 0
    assert result.error is None


async def test_a_service_failure_comes_back_on_the_result() -> None:
    """`_tick` catches it. Asserting it here means the wrapper is not bypassed."""
    service = StubService(raises=PayzenoLedgerError("bank feed unavailable"))
    job = FundingMatchJob(funding=service, settings=Settings())

    result = await job._tick()  # noqa: SLF001

    assert result.error is not None
    assert result.items_processed == 0


async def test_the_tolerance_is_configuration_not_a_constant() -> None:
    """Treasury renegotiated the wire fee twice last year.

    Both times the fix was an environment variable, which is the only reason it was a
    same-day fix rather than a release.
    """
    settings = Settings()

    assert settings.funding_match_tolerance_bps == 5
