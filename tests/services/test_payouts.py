"""`PayoutService` and `PayoutCalculator` — app/services/payouts.py.

Reassigned to me when Michal left. Her tests were thin here and the formula in
`domain-model.md` §9 has three things people get wrong every time, so the worked example
from that section is transcribed below as a table-driven test rather than paraphrased.

The three:

* reserve is **not** subtracted — `capture` credited `reserve` instead of
  `merchant_payable`, so the money was never in the payable balance to begin with
* open dispute liability is **not** subtracted — the `dispute` posting already moved it
  out of `merchant_payable`, and subtracting it here deducts the same dispute twice
* in-flight payouts **are** subtracted, or a merchant can drain the same balance twice by
  clicking twice

And one lock ordering fact: `PayoutService` is `AdvisoryLockManager.acquire_item_lock`'s
only caller in this repository, keyed on `payout_id`. `RetryScheduler` never calls it —
that is the distinction PAY-2041 turned on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.db.locks import AdvisoryLockManager
from app.domain.money import Money
from app.errors import (
    BankAccountUnusableError,
    InsufficientBalanceError,
    PayoutBlockedError,
)
from app.services.payouts import PayoutCalculator, PayoutService
from tests.doubles import CollectingPublisher, FrozenClock, StaticFeatureFlags
from tests.factories import make_merchant_projection

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 9, 0, tzinfo=UTC)
TODAY = date(2026, 4, 16)


class RecordingLocks(AdvisoryLockManager):
    def __init__(self) -> None:
        self.merchant_currency: list[str] = []
        self.item_locks: list[str] = []

    async def acquire_item_lock(self, session: Any, item_id: str) -> None:
        self.item_locks.append(item_id)


class StubEntries:
    """`sum_by_account_and_purpose` — the arc PERF query behind the calculator."""

    async def sum_by_account_and_purpose(
        self,
        session: Any,
        *,
        merchant_id: str,
        currency: str,
        account_type: str,
        cutoff: datetime,
        funded_only: bool = True,
    ) -> tuple[int, int]:
        self.calls.append(
            {"merchant_id": merchant_id, "account_type": account_type, "cutoff": cutoff}
        )
        return self.credits, self.debits


class StubPayouts:
    async def sum_in_flight(
        self, session: Any, *, merchant_id: str, currency: str
    ) -> int:
        return self.in_flight

    async def add(self, session: Any, obj: Any) -> Any:
        self.rows[obj.id] = obj
        self.added.append(obj)
        return obj

    def __init__(self, merchant: Any) -> None:
        self.merchant = merchant

    *,
    merchant_status: str = "active",
    credits: int = 100_000,
    debits: int = 0,
    initiator: StubInitiator | None = None,
):
    entries = StubEntries(credits=credits, debits=debits)
    payouts = StubPayouts(in_flight=in_flight)
    publisher = CollectingPublisher()
    available = await calculator.compute_available(object(), "mer_payout", "USD", NOW)

    assert available == Money(amount_minor=4_180, currency="USD")


async def test_compute_available_subtracts_payouts_already_in_flight() -> None:
    """Click the button twice, get paid once.

    Without this the second `create_payout` sees the same balance the first one did.
    """
    _, calculator, _, _, _, _ = _service(credits=9_680, debits=5_500, in_flight=4_180)

    payout = await service.mark_failed(
        object(),
        payout_id,
        failure_code="account_closed",
        failure_message="R02 account closed",
    )

    assert payout.status == "failed"
    assert any(post["purpose"] == "payout_reversal" for post in ledger.posts)


async def test_mark_failed_emits_payout_failed(_seeded_payout) -> None:
    """The console learns about this over the WebSocket relay, so it has to be published."""
    service, payouts, payout_id = _seeded_payout
    payout = await service.create_payout(
        object(),
        merchant_id="mer_payout",
        req={"amount_minor": 5_000, "currency": "USD", "method": "ach"},
    )
    return service, payouts, payout.id
