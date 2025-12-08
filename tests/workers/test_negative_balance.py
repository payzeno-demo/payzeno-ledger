"""`NegativeBalanceJob` — app/workers/negative_balance.py.

Daily. Finds merchants whose `merchant_payable` has gone negative — usually a run of
chargebacks after the payouts have already gone out — and pulls the shortfall back by ACH
debit.

There is a `# FIXME` in the job body saying the threshold is hardcoded and should come
from `merchant.risk_tier`. It should. A high-risk merchant sitting $50 negative is a
different situation from a low-risk one, and today they are treated identically. The test
below pins the current constant so that when somebody finally wires the risk tier through,
they get a failing test telling them exactly which behaviour they changed rather than
discovering it from a merchant complaint.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.errors import BankAccountUnusableError
from app.workers.negative_balance import (
    BATCH_SIZE,
    NEGATIVE_THRESHOLD_MINOR,
    NegativeBalanceJob,
)
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 4, 0, tzinfo=UTC)


class Row:
    def __init__(self, merchant_id: str, available_minor: int, currency: str = "USD") -> None:
        self.merchant_id = merchant_id
        self.currency = currency
        self.available_minor = available_minor
        self.livemode = True


class StubBalances:
    """`MerchantBalanceCacheRepository.list_negative`.

    The threshold is applied *here*, not in the job — the query is
    ``available_minor <= -threshold_minor`` against `merchant_balance_cache`, and pushing
    it down is what keeps the job from reading every merchant every night. The stub
    applies it the same way so a test about the threshold is a test about the behaviour
    rather than about which layer happens to filter.
    """

    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int]] = []

    async def list_negative(
        self, session: Any, *, threshold_minor: int, limit: int
    ) -> list[Row]:
        self.calls.append((threshold_minor, limit))
        found = [row for row in self.rows if row.available_minor <= -threshold_minor]
        return found[:limit]


class StubRepositories:
    """The job reaches one attribute off the container's repository namespace."""

    def __init__(self, balances: StubBalances) -> None:
        self.balance_cache = balances


class StubPuller:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.pulls: list[tuple[str, int]] = []
        self.fail_on = fail_on or set()

    async def pull(
        self,
        session: Any,
        *,
        merchant_id: str,
        currency: str,
        amount_minor: int,
        reason: str,
    ) -> Any:
        if merchant_id in self.fail_on:
            raise BankAccountUnusableError("account closed", merchant_id=merchant_id)
        self.pulls.append((merchant_id, amount_minor))
        return type(
            "DebitPullResult",
            (),
            {
                "rail_reference": f"ACH-DR-{merchant_id}",
                "amount_minor": amount_minor,
                "effective_date": date(2026, 4, 17),
                "submitted_at": NOW,
                "transaction_id": "txn_pull",
            },
        )()


class Settings:
    negative_balance_interval_seconds = 86400
    negative_balance_batch_size = 200


def _job(rows: list[Row], puller: StubPuller, sessions):
    balances = StubBalances(rows)
    job = NegativeBalanceJob(
        sessions=sessions,
        puller=puller,
        repositories=StubRepositories(balances),
        clock=FrozenClock(NOW),
        settings=Settings(),
    )
    return job, balances


async def test_interval_is_daily(sessions_factory) -> None:
    job, _ = _job([], StubPuller(), sessions_factory)

    assert job.interval_seconds == 86400
    assert job.name == "negative_balance"


async def test_it_pulls_the_shortfall(sessions_factory) -> None:
    puller = StubPuller()
    job, balances = _job([Row("mer_neg", -75_000)], puller, sessions_factory)

    result = await job.run_once()

    assert puller.pulls == [("mer_neg", 75_000)]
    assert balances.calls == [(NEGATIVE_THRESHOLD_MINOR, BATCH_SIZE)]
    assert result.items_processed == 1


async def test_a_balance_inside_the_threshold_is_left_alone(sessions_factory) -> None:
    """Pulling $2 by ACH costs more than $2 and annoys the merchant more than that."""
    puller = StubPuller()
    job, _ = _job(
        [Row("mer_small", -(NEGATIVE_THRESHOLD_MINOR - 1))], puller, sessions_factory
    )

    result = await job.run_once()

    assert puller.pulls == []
    assert result.items_processed == 0


async def test_the_threshold_is_still_a_constant(sessions_factory) -> None:
    """FIXME in the job body: this should come from `merchant.risk_tier`.

    Pinned deliberately. When somebody wires the risk tier through, this test fails and
    tells them which behaviour they are changing.
    """
    assert NEGATIVE_THRESHOLD_MINOR == 5_000_00


async def test_a_positive_balance_is_never_touched(sessions_factory) -> None:
    puller = StubPuller()
    job, _ = _job([Row("mer_fine", 40_000)], puller, sessions_factory)

    result = await job.run_once()

    assert puller.pulls == []
    assert result.items_processed == 0


async def test_an_unusable_account_does_not_stop_the_other_merchants(
    sessions_factory,
) -> None:
    puller = StubPuller(fail_on={"mer_closed"})
    job, _ = _job(
        [Row("mer_closed", -80_000), Row("mer_ok", -120_000)], puller, sessions_factory
    )

    result = await job.run_once()

    assert puller.pulls == [("mer_ok", 120_000)]
    assert result.items_processed == 1
