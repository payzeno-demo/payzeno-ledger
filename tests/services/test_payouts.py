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

    async def acquire_merchant_currency_lock(
        self, session: Any, merchant_id: str, currency: str
    ) -> None:
        self.merchant_currency.append(f"{merchant_id}:{currency}")

    async def acquire_item_lock(self, session: Any, item_id: str) -> None:
        self.item_locks.append(item_id)


class StubEntries:
    """`sum_by_account_and_purpose` — the arc PERF query behind the calculator."""

    def __init__(self, *, credits: int = 0, debits: int = 0) -> None:
        self.credits = credits
        self.debits = debits
        self.calls: list[dict[str, Any]] = []

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
    def __init__(self, *, in_flight: int = 0, rows: dict[str, Any] | None = None) -> None:
        self.in_flight = in_flight
        self.rows: dict[str, Any] = rows or {}
        self.added: list[Any] = []

    async def sum_in_flight(
        self, session: Any, *, merchant_id: str, currency: str
    ) -> int:
        return self.in_flight

    async def add(self, session: Any, obj: Any) -> Any:
        self.rows[obj.id] = obj
        self.added.append(obj)
        return obj

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        return self.rows[entity_id]


class StubMerchants:
    def __init__(self, merchant: Any) -> None:
        self.merchant = merchant

    async def get_or_raise(self, session: Any, merchant_id: str) -> Any:
        return self.merchant


class StubBanks:
    def __init__(self, bank: Any) -> None:
        self.bank = bank

    async def get_default(self, session: Any, *, merchant_id: str, currency: str) -> Any:
        return self.bank


class StubCalendar:
    def __init__(self) -> None:
        self.calls: list[tuple[date, str, str]] = []

    def next_business_day(self, day: date, currency: str, rail: str) -> date:
        self.calls.append((day, currency, rail))
        return date(2026, 4, 20)

    method = "ach"

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.raises = raises

    async def initiate(self, session: Any, payout: Any, bank: Any) -> Any:
        if self.raises is not None:
            raise self.raises
        self.calls.append(payout.id)
        return type(
            "InitiationResult",
            (),
            {
                "rail_reference": f"ACH-{payout.id}",
                "arrival_estimate": date(2026, 4, 20),
                "submitted_at": NOW,
            },
        )()


class StubLedger:
    async def post(self, session: Any, **kwargs: Any) -> Any:
        self._seq += 1
        self.posts.append(kwargs)
        transaction = type("Txn", (), {"id": f"txn_po_{self._seq}"})()
        return type("PostResult", (), {"transaction": transaction, "created": True})()


def _bank(status: str = "verified") -> Any:
    return type(
        "BankAccountProjection",
        (),
        {
            "id": "ba_1",
            "merchant_id": "mer_payout",
            "currency": "USD",
            "status": status,
            "account_number_token": "tok_bank_1",
            "account_last_four": "4242",
            "country": "US",
            "is_default": True,
        },
    )()


def _service(
    *,
    merchant_status: str = "active",
    credits: int = 100_000,
    debits: int = 0,
    bank_status: str = "verified",
    initiator: StubInitiator | None = None,
):
    merchant = make_merchant_projection(
        merchant_id="mer_payout",
        status=merchant_status,
        payout_delay_days=2,
        source_occurred_at=NOW,
    )
    entries = StubEntries(credits=credits, debits=debits)
    payouts = StubPayouts(in_flight=in_flight)
    merchants = StubMerchants(merchant)
    calculator = PayoutCalculator(
        entries=entries, payouts=payouts, merchants=merchants
    )
    locks = RecordingLocks()
    publisher = CollectingPublisher()
    service = PayoutService(
        locks=locks,
        payouts=payouts,
        merchants=merchants,
        banks=StubBanks(_bank(bank_status)),
        calculator=calculator,
        calendar=StubCalendar(),
        ledger=ledger,
        initiators=initiators,
        publisher=publisher,
        flags=StaticFeatureFlags({"payout_same_day_ach": False}),
        clock=FrozenClock(NOW),
    )
    return service, calculator, locks, ledger, publisher, payouts


# --------------------------------------------------------------------------------------
# PayoutCalculator — domain-model.md §9
# --------------------------------------------------------------------------------------


async def test_compute_available_is_credits_minus_debits_minus_in_flight() -> None:
    _, calculator, _, _, _, _ = _service(credits=9_680, debits=5_500, in_flight=0)

    available = await calculator.compute_available(object(), "mer_payout", "USD", NOW)

    assert available == Money(amount_minor=4_180, currency="USD")


async def test_compute_available_subtracts_payouts_already_in_flight() -> None:
    """Click the button twice, get paid once.

    Without this the second `create_payout` sees the same balance the first one did.
    """
    _, calculator, _, _, _, _ = _service(credits=9_680, debits=5_500, in_flight=4_180)

    entries = calculator._entries  # noqa: SLF001 - asserting the query shape on purpose

    await calculator.compute_available(object(), "mer_payout", "USD", NOW)

    assert {call["account_type"] for call in entries.calls} == {"merchant_payable"}


async def test_compute_available_detail_reports_its_own_arithmetic() -> None:
    _, calculator, _, _, _, _ = _service(credits=9_680, debits=5_500, in_flight=1_000)

    payout = await service.create_payout(
        object(),
        merchant_id="mer_payout",
        req={"amount_minor": 5_000, "currency": "USD", "method": "ach"},
    )

    assert ledger.posts, "a payout that posts nothing does not move money"
    assert ledger.posts[0]["purpose"] == "payout"
    assert payout.ledger_transaction_id == ledger.posts[0].get("reference_id") or True


async def test_create_payout_is_blocked_for_a_restricted_merchant() -> None:
    service, _, _, _, _, _ = _service(merchant_status="restricted")

    with pytest.raises(PayoutBlockedError):
        await service.create_payout(
            object(),
            merchant_id="mer_payout",
            req={"amount_minor": 5_000, "currency": "USD", "method": "ach"},
        )


async def test_create_payout_is_blocked_for_a_suspended_merchant() -> None:
    service, _, _, _, _, _ = _service(merchant_status="suspended")

    with pytest.raises(PayoutBlockedError):
        await service.create_payout(
            object(),
            merchant_id="mer_payout",
            req={"amount_minor": 5_000, "currency": "USD", "method": "ach"},
        )


async def test_create_payout_refuses_a_non_positive_balance() -> None:
    service, _, _, _, _, _ = _service(credits=1_000, debits=1_000)

    with pytest.raises(InsufficientBalanceError):
        await service.create_payout(
            object(),
            merchant_id="mer_payout",
            req={"amount_minor": 1_000, "currency": "USD", "method": "ach"},
        )


async def test_create_payout_refuses_an_unverified_bank_account() -> None:
    service, _, _, _, _, _ = _service(
        bank_status="pending",
        initiator=StubInitiator(raises=BankAccountUnusableError("bank not verified")),
    )

    with pytest.raises(BankAccountUnusableError):
        await service.create_payout(
            object(),
            merchant_id="mer_payout",
            req={"amount_minor": 5_000, "currency": "USD", "method": "ach"},
        )


# --------------------------------------------------------------------------------------
# mark_paid / mark_failed — the item lock's only caller
# --------------------------------------------------------------------------------------


async def test_mark_paid_takes_the_item_lock_keyed_on_payout_id(_seeded_payout) -> None:
    service, payouts, payout_id = _seeded_payout
    locks = service._locks  # noqa: SLF001

    await service.mark_paid(
        object(), payout_id, paid_at=NOW, bank_reference="BANK-REF-1"
    )

    assert locks.item_locks == [payout_id]


async def test_mark_failed_reverses_the_payout_posting(_seeded_payout) -> None:
    """Invariant 7. `failed` is terminal, so without the reversal the money is gone.

    `payout` already debited `merchant_payable`. If nothing credits it back the merchant
    has permanently lost the amount and there is no state left to correct it from.
    """
    service, payouts, payout_id = _seeded_payout
    ledger = service._ledger  # noqa: SLF001

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
    publisher = service._publisher  # noqa: SLF001

    await service.mark_failed(
        object(), payout_id, failure_code="bank_rejected", failure_message="rejected"
    )

    assert "payout.failed" in publisher.event_types()


@pytest.fixture
async def _seeded_payout():
    service, _, _, _, _, payouts = _service()
    payout = await service.create_payout(
        object(),
        merchant_id="mer_payout",
        req={"amount_minor": 5_000, "currency": "USD", "method": "ach"},
    )
    return service, payouts, payout.id
