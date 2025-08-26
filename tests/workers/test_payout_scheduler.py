"""`PayoutSchedulerJob` — app/workers/payout_scheduler.py.

Hourly. Finds payouts whose `available_on` has arrived and hands them to the rail.

Michal wrote this and it went cold about eight weeks ago; the tickets came to me. The one
thing I had to work out from scratch is why it is hourly rather than daily: the four rails
have four different cutoffs (21:00, 16:45, 14:00 and 17:30 UTC) and a daily job would have
to pick one of them and be wrong for three. Hourly plus a per-rail cutoff check in
`BankingCalendar` is the cheap version of four schedules.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.errors import BankAccountUnusableError, PayoutBlockedError
from app.workers.payout_scheduler import PayoutSchedulerJob
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 15, 0, tzinfo=UTC)
TODAY = date(2026, 4, 16)


def _payout(payout_id: str, method: str = "ach", available_on: date = TODAY) -> Any:
    return type(
        "Payout",
        (),
        {
            "id": payout_id,
            "merchant_id": "mer_sched",
            "currency": "USD",
            "method": method,
            "status": "scheduled",
            "available_on": available_on,
            "amount_minor": 50_000,
        },
    )()


class StubPayoutService:
    def __init__(self, due: list[Any], *, fail_on: dict[str, Exception] | None = None) -> None:
        self.due = due
        self.fail_on = fail_on or {}
        self.initiated: list[str] = []
        self.asked_for: list[date] = []

    async def due_payouts(self, session: Any, *, on: date) -> list[Any]:
        self.asked_for.append(on)
        return [payout for payout in self.due if payout.available_on <= on]

    async def initiate_payout(self, session: Any, payout_id: str) -> Any:
        if payout_id in self.fail_on:
            raise self.fail_on[payout_id]
        self.initiated.append(payout_id)
        return _payout(payout_id)


class Settings:
    payout_scheduler_interval_seconds = 3600


def _job(service: StubPayoutService, sessions) -> PayoutSchedulerJob:
    return PayoutSchedulerJob(
        sessions=sessions, payouts=service, settings=Settings(), clock=FrozenClock(NOW)
    )


async def test_interval_is_hourly(sessions_factory) -> None:
    job = _job(StubPayoutService([]), sessions_factory)

    assert job.interval_seconds == 3600
    assert job.name == "payout_scheduler"


async def test_it_initiates_every_due_payout(sessions_factory) -> None:
    service = StubPayoutService([_payout("po_1"), _payout("po_2")])
    job = _job(service, sessions_factory)

    result = await job.run_once()

    assert service.initiated == ["po_1", "po_2"]
    assert result.items_processed == 2


async def test_it_leaves_a_future_payout_alone(sessions_factory) -> None:
    """`available_on` is a date the calendar computed. Paying early is not a favour —
    the funds are not there yet."""
    service = StubPayoutService([_payout("po_future", available_on=date(2026, 4, 20))])
    job = _job(service, sessions_factory)

    result = await job.run_once()

    assert service.initiated == []
    assert result.items_processed == 0


async def test_it_asks_for_today(sessions_factory) -> None:
    service = StubPayoutService([])
    job = _job(service, sessions_factory)

    await job.run_once()

    assert service.asked_for == [TODAY]


async def test_one_unusable_bank_account_does_not_stop_the_rest(sessions_factory) -> None:
    """One merchant closing their bank account should not hold up everyone else's money.

    The failure lands on that payout's own row and the merchant gets an email; the other
    payouts go out on the same tick.
    """
    service = StubPayoutService(
        [_payout("po_bad"), _payout("po_good")],
        fail_on={"po_bad": BankAccountUnusableError("account closed")},
    )
    job = _job(service, sessions_factory)

    result = await job.run_once()

    assert service.initiated == ["po_good"]
    assert result.items_processed == 1


async def test_a_blocked_merchant_is_skipped_not_retried_forever(sessions_factory) -> None:
    service = StubPayoutService(
        [_payout("po_blocked")],
        fail_on={"po_blocked": PayoutBlockedError("merchant restricted")},
    )
    job = _job(service, sessions_factory)

    result = await job.run_once()

    assert result.items_processed == 0
    assert result.error is None
