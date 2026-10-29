"""Converted payouts — `PayoutService` with RFC platform-v2 §7.

`CreatePayoutRequest.currency` is the balance being paid out. When the bank account is in
another currency the payout converts at the provider mid-rate less the payout markup, and
the ledger posts two transactions — one per currency, each balanced through
`fx_clearing` — instead of one. What is checked here:

* which bank account a payout goes to, and so whether it converts at all;
* that the lock, the in-flight check and the balance read are all on the *balance*
  currency, not the bank's;
* that every currency's legs balance on their own, and the spread lands in `fx_revenue`;
* that a failure or return mirrors both legs, so the merchant's EUR comes back in EUR.

Stubs rather than the real repositories: this is the service's arithmetic and ordering,
and `tests/integration/` owns what Postgres does with it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.domain.money import Money
from app.errors import (
    BankAccountProjectionNotFoundError,
    InsufficientBalanceError,
    PayoutBlockedError,
    ValidationError,
)
from app.flags import StaticFeatureFlags
from app.ports import FxRates, InitiationResult
from app.services.fx import FxRateService
from app.services.payouts import AvailableFunds, PayoutService
from tests.doubles import CollectingPublisher, FrozenClock
from tests.factories import make_bank_account_projection, make_merchant_projection

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
MERCHANT = "mer_fx_payouts"

SIM_RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("0.9231"),
    "GBP": Decimal("0.7902"),
    "ILS": Decimal("3.7120"),
    "JPY": Decimal("151.20"),
}


class StaticProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def latest(self, base: str) -> FxRates:
        self.calls += 1
        return FxRates(base=base, as_of=NOW, rates=SIM_RATES)


class Locks:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.merchant_currency: list[str] = []

    async def acquire_merchant_currency_lock(
        self, session: Any, merchant_id: str, currency: str
    ) -> None:
        self.events.append("lock")
        self.merchant_currency.append(f"{merchant_id}:{currency}")

    async def acquire_item_lock(self, session: Any, item_id: str) -> None:
        self.events.append("item_lock")


class Payouts:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.rows: dict[str, Any] = {}
        self.in_flight: Any = None
        self.in_flight_asked: list[str] = []

    async def find_in_flight(
        self, session: Any, *, merchant_id: str, currency: str, livemode: bool
    ) -> Any:
        self.events.append("in_flight")
        self.in_flight_asked.append(currency)
        return self.in_flight

    async def add(self, session: Any, obj: Any) -> Any:
        self.rows[obj.id] = obj
        return obj

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        return self.rows[entity_id]

    async def mark_returned(self, session: Any, payout_id: str, **kwargs: Any) -> Any:
        payout = self.rows[payout_id]
        payout.status = "returned"
        payout.failure_code = kwargs["failure_code"]
        payout.reversal_transaction_id = kwargs["reversal_transaction_id"]
        payout.returned_at = kwargs["at"]
        return payout


class Merchants:
    def __init__(self, merchant: Any) -> None:
        self.merchant = merchant

    async def get_or_raise(self, session: Any, merchant_id: str) -> Any:
        return self.merchant


class Banks:
    def __init__(self, accounts: list[Any]) -> None:
        self.accounts = accounts

    async def get_or_raise(self, session: Any, bank_account_id: str) -> Any:
        return next(a for a in self.accounts if a.bank_account_id == bank_account_id)

    async def find_default(
        self, session: Any, *, merchant_id: str, currency: str, livemode: bool
    ) -> Any:
        return next((a for a in self.accounts if a.is_default and a.currency == currency), None)

    async def list_defaults(self, session: Any, *, merchant_id: str, livemode: bool) -> list[Any]:
        return sorted((a for a in self.accounts if a.is_default), key=lambda a: a.currency)


class Calculator:
    def __init__(self, events: list[str], balances: dict[str, int]) -> None:
        self.events = events
        self.balances = balances
        self.asked: list[str] = []

    async def compute_available_detail(
        self,
        session: Any,
        merchant_id: str,
        currency: str,
        cutoff: datetime,
        *,
        livemode: bool = True,
    ) -> AvailableFunds:
        self.events.append("balance")
        self.asked.append(currency)
        amount = self.balances.get(currency, 0)
        return AvailableFunds(
            amount=Money(amount, currency), posted_minor=amount, in_flight_minor=0
        )


class Calendar:
    def next_business_day(self, day: date, currency: str, rail: str) -> date:
        return date(2026, 10, 30)


class Initiator:
    def __init__(self, method: str) -> None:
        self.method = method
        self.payouts: list[Any] = []

    async def initiate(self, session: Any, payout: Any, bank: Any) -> InitiationResult:
        self.payouts.append(payout)
        return InitiationResult(
            rail_reference=f"{self.method.upper()}-{payout.id[-6:]}",
            arrival_estimate=date(2026, 10, 31),
            submitted_at=NOW,
        )


class Ledger:
    """Records each `post`; checks every transaction balances in its own currency."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, session: Any, **kwargs: Any) -> Any:
        lines = kwargs["lines"]
        debits = sum(line.amount_minor for line in lines if line.direction == "debit")
        credits = sum(line.amount_minor for line in lines if line.direction == "credit")
        assert debits == credits, kwargs
        self.posts.append(kwargs)
        transaction = type("Txn", (), {"id": f"txn_{len(self.posts)}"})()
        return type("PostResult", (), {"transaction": transaction, "created": True})()

    def legs(self, index: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for line in self.posts[index]["lines"]:
            out[line.account_type] = out.get(line.account_type, 0) + line.signed_minor()
        return out


def _bank(bank_id: str, currency: str, scheme: str, *, default: bool = True) -> Any:
    return make_bank_account_projection(
        bank_account_id=bank_id,
        merchant_id=MERCHANT,
        currency=currency,
        scheme=scheme,
        is_default=default,
    )


class World:
    def __init__(
        self,
        *,
        banks: list[Any],
        balances: dict[str, int],
        default_currency: str = "USD",
        fx: bool = True,
    ) -> None:
        self.events: list[str] = []
        merchant = make_merchant_projection(merchant_id=MERCHANT, source_occurred_at=NOW)
        merchant.default_currency = default_currency
        self.locks = Locks(self.events)
        self.payouts = Payouts(self.events)
        self.calculator = Calculator(self.events, balances)
        self.ledger = Ledger()
        self.publisher = CollectingPublisher()
        self.provider = StaticProvider()
        self.initiators = {m: Initiator(m) for m in ("standard_ach", "sepa", "faster_payments")}
        self.service = PayoutService(
            locks=self.locks,
            payouts=self.payouts,
            merchants=Merchants(merchant),
            banks=Banks(banks),
            calculator=self.calculator,
            calendar=Calendar(),
            ledger=self.ledger,
            initiators=self.initiators,
            publisher=self.publisher,
            flags=StaticFeatureFlags(),
            clock=FrozenClock(NOW),
            fx=FxRateService(self.provider, FrozenClock(NOW), payout_markup_bps=100)
            if fx
            else None,
        )

    async def pay(self, **req: Any) -> Any:
        return await self.service.create_payout(object(), merchant_id=MERCHANT, req=req)


# -- choosing the account, and so whether it converts --------------------------------------


async def test_a_same_currency_default_means_no_conversion() -> None:
    world = World(
        banks=[_bank("ba_usd", "USD", "aba"), _bank("ba_eur", "EUR", "iban")],
        balances={"EUR": 10_000},
    )

    payout = await world.pay(currency="EUR")

    assert payout.bank_account_id == "ba_eur"
    assert (payout.amount_minor, payout.currency) == (10_000, "EUR")
    assert payout.source_currency is None and payout.fx_rate is None
    assert payout.method == "sepa"
    assert world.provider.calls == 0
    assert [post["currency"] for post in world.ledger.posts] == ["EUR"]
    assert world.ledger.legs(0) == {"merchant_payable": 10_000, "cash": -10_000}


async def test_no_account_in_the_balance_currency_converts_into_the_merchant_default() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"EUR": 10_000})

    payout = await world.pay(currency="EUR")

    assert payout.bank_account_id == "ba_usd"
    assert (payout.amount_minor, payout.currency) == (10_725, "USD")
    assert (payout.source_amount_minor, payout.source_currency) == (10_000, "EUR")
    assert payout.fx_rate == Decimal("1.0724731882")
    assert payout.fx_spread_minor == 108
    assert payout.method == "standard_ach"


async def test_the_merchant_default_currency_wins_among_other_defaults() -> None:
    world = World(
        banks=[_bank("ba_gbp", "GBP", "uk_sort_code"), _bank("ba_usd", "USD", "aba")],
        balances={"JPY": 100_000},
        default_currency="USD",
    )

    payout = await world.pay(currency="JPY")

    assert payout.bank_account_id == "ba_usd"
    assert payout.currency == "USD"


async def test_a_named_account_in_another_currency_converts() -> None:
    world = World(
        banks=[
            _bank("ba_usd", "USD", "aba"),
            _bank("ba_gbp", "GBP", "uk_sort_code", default=False),
        ],
        balances={"USD": 50_000},
    )

    payout = await world.pay(currency="USD", bank_account_id="ba_gbp")

    assert payout.currency == "GBP"
    assert payout.source_currency == "USD"
    assert payout.method == "faster_payments"
    # USD 500.00 x 0.7902 x 0.99 = GBP 391.149 -> 391.15
    assert payout.amount_minor == 39_115


async def test_another_merchants_account_is_refused() -> None:
    stranger = make_bank_account_projection(
        bank_account_id="ba_other", merchant_id="mer_someone_else", currency="USD"
    )
    world = World(banks=[_bank("ba_usd", "USD", "aba"), stranger], balances={"USD": 1_000})

    with pytest.raises(ValidationError):
        await world.pay(currency="USD", bank_account_id="ba_other")
    assert world.ledger.posts == []


async def test_no_default_account_at_all_is_a_404() -> None:
    world = World(banks=[], balances={"EUR": 1_000})

    with pytest.raises(BankAccountProjectionNotFoundError):
        await world.pay(currency="EUR")


async def test_a_conversion_without_an_fx_provider_is_refused() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"EUR": 1_000}, fx=False)

    with pytest.raises(ValidationError):
        await world.pay(currency="EUR")
    assert world.ledger.posts == []


# -- locking on the balance, not the bank -------------------------------------------------


async def test_lock_in_flight_check_and_balance_are_all_on_the_balance_currency() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"EUR": 10_000})

    await world.pay(currency="EUR")

    assert world.locks.merchant_currency == [f"{MERCHANT}:EUR"]
    assert world.payouts.in_flight_asked == ["EUR"]
    assert world.calculator.asked == ["EUR"]
    assert world.events[:3] == ["lock", "in_flight", "balance"]


async def test_a_payout_already_in_flight_for_the_balance_is_a_clear_refusal() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"EUR": 10_000})
    world.payouts.in_flight = type("Payout", (), {"id": "po_earlier", "status": "in_transit"})()

    with pytest.raises(PayoutBlockedError) as raised:
        await world.pay(currency="EUR")

    assert raised.value.details["in_flight_payout_id"] == "po_earlier"
    assert world.ledger.posts == []


async def test_a_partial_amount_is_in_the_balance_currency() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"EUR": 10_000})

    payout = await world.pay(currency="EUR", amount_minor=4_000)

    assert payout.source_amount_minor == 4_000
    with pytest.raises(InsufficientBalanceError):
        await World(banks=[_bank("ba_usd", "USD", "aba")], balances={"EUR": 10_000}).pay(
            currency="EUR", amount_minor=10_001
        )


# -- the postings -------------------------------------------------------------------------


async def test_a_converted_payout_posts_one_balanced_transaction_per_currency() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"EUR": 10_000})

    payout = await world.pay(currency="EUR")

    source, target = world.ledger.posts
    assert (source["currency"], target["currency"]) == ("EUR", "USD")
    assert source["purpose"] == target["purpose"] == "payout"
    assert source["reference_id"] == target["reference_id"] == payout.id
    assert source["idempotency_key"] != target["idempotency_key"]
    assert world.ledger.legs(0) == {"merchant_payable": 10_000, "fx_clearing": -10_000}
    assert world.ledger.legs(1) == {"fx_clearing": 10_833, "cash": -10_725, "fx_revenue": -108}
    # The payout points at the transaction that moved the merchant's money.
    assert payout.ledger_transaction_id == "txn_1"


async def test_the_rail_and_the_event_see_what_lands_in_the_bank() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"EUR": 10_000})

    payout = await world.pay(currency="EUR")

    initiated = world.initiators["standard_ach"].payouts[0]
    assert (initiated.amount_minor, initiated.currency) == (10_725, "USD")
    scheduled = world.publisher.payload_for("payout.scheduled")
    assert (scheduled["amount_minor"], scheduled["currency"]) == (10_725, "USD")
    assert payout.created_at == NOW


# -- reversals ------------------------------------------------------------------------------


async def test_a_failed_converted_payout_gives_the_balance_back_in_its_own_currency() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"EUR": 10_000})
    payout = await world.pay(currency="EUR")

    failed = await world.service.mark_failed(
        object(), payout.id, failure_code="account_closed", failure_message="R02"
    )

    reversals = [p for p in world.ledger.posts if p["purpose"] == "payout_reversal"]
    assert [r["currency"] for r in reversals] == ["USD", "EUR"]
    assert world.ledger.legs(2) == {"cash": 10_725, "fx_revenue": 108, "fx_clearing": -10_833}
    assert world.ledger.legs(3) == {"fx_clearing": 10_000, "merchant_payable": -10_000}
    assert failed.status == "failed"
    assert failed.reversal_transaction_id == "txn_4"


async def test_every_account_nets_to_zero_after_a_return() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"JPY": 150_000})
    payout = await world.pay(currency="JPY")
    payout.status = "paid"

    returned = await world.service.mark_failed(
        object(), payout.id, failure_code="account_closed", failure_message="R02"
    )

    net: dict[tuple[str, str], int] = {}
    for post in world.ledger.posts:
        for line in post["lines"]:
            key = (post["currency"], line.account_type)
            net[key] = net.get(key, 0) + line.signed_minor()
    assert set(net.values()) == {0}
    assert returned.status == "returned"
    assert "payout.returned" in world.publisher.event_types()


async def test_cancelling_a_converted_payout_reverses_both_legs() -> None:
    world = World(banks=[_bank("ba_usd", "USD", "aba")], balances={"GBP": 2_000})
    payout = await world.pay(currency="GBP")

    await world.service.cancel_payout(object(), payout.id)

    assert [p["purpose"] for p in world.ledger.posts].count("payout_reversal") == 2
