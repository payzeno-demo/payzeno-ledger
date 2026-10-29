"""`PayoutSweepJob` — app/workers/payout_sweep.py.

RFC platform-v2 §7: scheduled payouts sweep every non-zero currency balance to the
merchant's bank account, converting when the account is in another currency. The job only
decides *who is due*; `PayoutService.create_payout` decides everything about the payout
itself, so it is stubbed here and asserted on by what it was asked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.errors import BankAccountProjectionNotFoundError, PayoutBlockedError
from app.workers.payout_sweep import PayoutSweepJob, period_start
from tests.doubles import FrozenClock
from tests.factories import make_merchant_projection

pytestmark = pytest.mark.asyncio

# A Wednesday.
NOW = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)


class Balance:
    def __init__(self, merchant_id: str, currency: str, available: int, livemode: bool = True):
        self.merchant_id = merchant_id
        self.currency = currency
        self.available_minor = available
        self.livemode = livemode


class Repos:
    def __init__(self, balances: list[Balance], merchants: list[Any]) -> None:
        self.balance_cache = self
        self.merchants = self
        self.payouts = self
        self._balances = balances
        self._merchants = {m.merchant_id: m for m in merchants}
        self.last: dict[tuple[str, str], datetime] = {}

    async def list_payable(self, session: Any, *, limit: int) -> list[Balance]:
        return [b for b in self._balances if b.available_minor > 0][:limit]

    async def get(self, session: Any, merchant_id: str) -> Any:
        return self._merchants.get(merchant_id)

    async def last_created_at(
        self, session: Any, *, merchant_id: str, currency: str, livemode: bool
    ) -> datetime | None:
        return self.last.get((merchant_id, currency))


class Payouts:
    def __init__(self, raises: dict[tuple[str, str], Exception] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.raises = raises or {}

    async def create_payout(self, session: Any, *, merchant_id: str, req: dict[str, Any]) -> Any:
        self.requests.append((merchant_id, req))
        error = self.raises.get((merchant_id, req["currency"]))
        if error is not None:
            raise error
        return type(
            "Payout",
            (),
            {
                "id": f"po_{merchant_id}_{req['currency']}",
                "merchant_id": merchant_id,
                "amount_minor": 1,
                "currency": "USD",
                "source_currency": req["currency"],
                "source_amount_minor": 1,
            },
        )()


class Sessions:
    def __init__(self) -> None:
        self.opened = 0

    def begin(self) -> Any:
        sessions = self

        class _Ctx:
            async def __aenter__(self) -> object:
                sessions.opened += 1
                return object()

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Ctx()


def _merchant(merchant_id: str, *, schedule: str = "daily", status: str = "active") -> Any:
    return make_merchant_projection(
        merchant_id=merchant_id, payout_schedule=schedule, status=status
    )


def _job(repos: Repos, payouts: Payouts, now: datetime = NOW) -> PayoutSweepJob:
    return PayoutSweepJob(
        sessions=Sessions(), payouts=payouts, repositories=repos, clock=FrozenClock(now)
    )


async def test_every_currency_balance_is_swept_with_no_amount_and_no_account() -> None:
    """Whole balance, default routing: the service picks the account and converts."""
    repos = Repos(
        [Balance("mer_a", "USD", 5_000), Balance("mer_a", "EUR", 7_000)],
        [_merchant("mer_a")],
    )
    payouts = Payouts()

    result = await _job(repos, payouts).run_once()

    assert payouts.requests == [
        ("mer_a", {"currency": "USD", "livemode": True}),
        ("mer_a", {"currency": "EUR", "livemode": True}),
    ]
    assert result.items_processed == 2
    assert result.error is None


async def test_manual_and_inactive_merchants_are_left_alone() -> None:
    repos = Repos(
        [Balance("mer_manual", "USD", 5_000), Balance("mer_restricted", "USD", 5_000)],
        [
            _merchant("mer_manual", schedule="manual"),
            _merchant("mer_restricted", status="restricted"),
        ],
    )
    payouts = Payouts()

    await _job(repos, payouts).run_once()

    assert payouts.requests == []


async def test_a_balance_already_paid_this_period_is_not_paid_again() -> None:
    repos = Repos(
        [Balance("mer_daily", "USD", 5_000), Balance("mer_weekly", "USD", 5_000)],
        [_merchant("mer_daily"), _merchant("mer_weekly", schedule="weekly")],
    )
    repos.last[("mer_daily", "USD")] = NOW - timedelta(hours=2)  # earlier today
    repos.last[("mer_weekly", "USD")] = NOW - timedelta(days=1)  # Tuesday, this week
    payouts = Payouts()

    await _job(repos, payouts).run_once()

    assert payouts.requests == []


async def test_a_clock_jump_past_the_period_start_makes_it_due_once() -> None:
    """Sunday to Wednesday in one advance: the weekly merchant is due this week."""
    repos = Repos(
        [Balance("mer_weekly", "EUR", 9_000)], [_merchant("mer_weekly", schedule="weekly")]
    )
    repos.last[("mer_weekly", "EUR")] = NOW - timedelta(days=10)
    payouts = Payouts()

    result = await _job(repos, payouts).run_once()

    assert [req["currency"] for _, req in payouts.requests] == ["EUR"]
    assert result.items_processed == 1


async def test_one_merchant_that_cannot_be_paid_does_not_stop_the_rest() -> None:
    repos = Repos(
        [
            Balance("mer_no_bank", "USD", 5_000),
            Balance("mer_in_flight", "EUR", 5_000),
            Balance("mer_fine", "GBP", 5_000),
        ],
        [_merchant("mer_no_bank"), _merchant("mer_in_flight"), _merchant("mer_fine")],
    )
    payouts = Payouts(
        raises={
            ("mer_no_bank", "USD"): BankAccountProjectionNotFoundError("no bank"),
            ("mer_in_flight", "EUR"): PayoutBlockedError("still in transit"),
        }
    )

    result = await _job(repos, payouts).run_once()

    assert len(payouts.requests) == 3
    assert result.items_processed == 1
    assert result.error is None


async def test_test_mode_balances_are_swept_in_test_mode() -> None:
    repos = Repos([Balance("mer_a", "USD", 5_000, livemode=False)], [_merchant("mer_a")])
    payouts = Payouts()

    await _job(repos, payouts).run_once()

    assert payouts.requests == [("mer_a", {"currency": "USD", "livemode": False})]


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        ("daily", datetime(2026, 10, 28, tzinfo=UTC)),
        ("weekly", datetime(2026, 10, 26, tzinfo=UTC)),
        ("monthly", datetime(2026, 10, 1, tzinfo=UTC)),
    ],
)
async def test_period_start(schedule: str, expected: datetime) -> None:
    assert period_start(schedule, NOW) == expected
