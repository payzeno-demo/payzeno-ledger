"""`MatchStrategy` and its four concretes — app/services/reconciliation/matcher.py.

Matching is where an acquirer line acquires a `charge_id`, and `charge_id` is what
decides whether the item can ever settle. Two properties are worth more than the rest:

* the strategies are tried in a fixed order and the first hit wins
* `HeuristicAmountWindowMatch` is deliberately NOT confident, so a heuristic hit lands
  `needs_review` and never settles on its own

Everything here is in-process. Real SQL for the same repository methods is asserted in
`tests/repositories/test_settlement_charge.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.errors import ChargeProjectionNotFoundError
from app.repositories.settlement_charge import SettlementChargeRepository
from app.services.reconciliation.matcher import (
    HEURISTIC_AMOUNT_SLACK_MINOR,
    ExactReferenceMatch,
    HeuristicAmountWindowMatch,
    ManualMatch,
    MatchStrategy,
    NetworkTransactionMatch,
    match_items,
)
from tests.doubles import FrozenClock
from tests.factories import make_charge_projection, make_item

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)


class LookupChargeRepository(SettlementChargeRepository):
    """The three lookups the strategies use, backed by a list."""

    def __init__(self, charges: list[Any] | None = None) -> None:
        super().__init__()
        self.rows = list(charges or [])
        self.calls: list[str] = []

    async def find_by_processor_reference(
        self, session: Any, *, acquirer: str, processor_reference: str
    ) -> Any | None:
        self.calls.append("find_by_processor_reference")
        for row in self.rows:
            if row.acquirer == acquirer and row.processor_reference == processor_reference:
                return row
        return None

    async def find_by_network_transaction(
        self, session: Any, *, acquirer: str, network_transaction_id: str
    ) -> Any | None:
        self.calls.append("find_by_network_transaction")
        for row in self.rows:
            if (
                row.acquirer == acquirer
                and row.network_transaction_id == network_transaction_id
            ):
                return row
        return None

    async def find_in_amount_window(
        self,
        session: Any,
        *,
        merchant_id: str,
        currency: str,
        amount_minor: int,
        slack_minor: int,
        authorized_from: datetime,
        authorized_to: datetime,
    ) -> list[Any]:
        self.calls.append("find_in_amount_window")
        return [
            row
            for row in self.rows
            if row.merchant_id == merchant_id
            and row.currency == currency
            and abs(row.amount_minor - amount_minor) <= slack_minor
            and authorized_from <= row.authorized_at <= authorized_to
        ]

    async def get_or_raise(self, session: Any, charge_id: str) -> Any:
        for row in self.rows:
            if row.charge_id == charge_id:
                return row
        raise ChargeProjectionNotFoundError(entity_id=charge_id)


def _charge(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "charge_id": "ch_match",
        "merchant_id": "mer_match",
        "currency": "USD",
        "amount_minor": 10_000,
        "acquirer": "worldflow",
        "processor_reference": "WF-1001",
        "network_transaction_id": "NTX-1001",
        "authorized_at": NOW,
        "source_occurred_at": NOW,
    }
    defaults.update(overrides)
    return make_charge_projection(**defaults)


def _item(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "item_id": "ri_match",
        "batch_id": "sb_match",
        "merchant_id": "mer_match",
        "currency": "USD",
        "acquirer": "worldflow",
        "acquirer_reference": "WF-1001",
        "network_reference": "NTX-1001",
        "gross_minor": 10_000,
        "charge_id": None,
        "line_type": "sale",
    }
    defaults.update(overrides)
    return make_item(**defaults)


async def test_every_strategy_declares_its_method_name() -> None:
    """`method` lands in `reconciliation_item.match_method` and ops reads that column."""
    methods = {
        ExactReferenceMatch.method,
        NetworkTransactionMatch.method,
        HeuristicAmountWindowMatch.method,
        ManualMatch.method,
    }
    assert methods == {
        "exact_reference",
        "network_transaction",
        "heuristic_amount_window",
        "manual",
    }
    for strategy in (
        ExactReferenceMatch,
        NetworkTransactionMatch,
        HeuristicAmountWindowMatch,
        ManualMatch,
    ):
        assert issubclass(strategy, MatchStrategy)


async def test_match_strategy_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        MatchStrategy()  # type: ignore[abstract]


async def test_exact_reference_matches_and_is_confident() -> None:
    charges = LookupChargeRepository([_charge()])

    outcome = await ExactReferenceMatch(charges).match(object(), _item())

    assert outcome.charge_id == "ch_match"
    assert outcome.method == "exact_reference"
    assert outcome.confident is True


async def test_exact_reference_abstains_across_acquirers() -> None:
    """References are only unique within an acquirer. `WF-1001` is not `NP-1001`."""
    charges = LookupChargeRepository([_charge(acquirer="nordpay")])

    outcome = await ExactReferenceMatch(charges).match(object(), _item())

    assert outcome.charge_id is None


async def test_network_transaction_catches_a_rewritten_reference() -> None:
    """Representments. Worldflow rewrites its own reference; the network id survives."""
    charges = LookupChargeRepository([_charge(processor_reference="WF-REPRESENTED")])
    item = _item(acquirer_reference="WF-1001", network_reference="NTX-1001")

    assert (await ExactReferenceMatch(charges).match(object(), item)).charge_id is None
    outcome = await NetworkTransactionMatch(charges).match(object(), item)

    assert outcome.charge_id == "ch_match"
    assert outcome.confident is True


async def test_network_transaction_abstains_without_a_network_reference() -> None:
    charges = LookupChargeRepository([_charge()])

    outcome = await NetworkTransactionMatch(charges).match(
        object(), _item(network_reference=None)
    )

    assert outcome.charge_id is None
    assert "find_by_network_transaction" not in charges.calls


async def test_heuristic_match_is_never_confident() -> None:
    """The property that keeps a wrong match from moving money."""
    charges = LookupChargeRepository([_charge(processor_reference="X", network_transaction_id="Y")])
    strategy = HeuristicAmountWindowMatch(charges, FrozenClock(NOW))

    outcome = await strategy.match(object(), _item(network_reference=None))

    assert outcome.charge_id == "ch_match"
    assert outcome.confident is False


async def test_heuristic_match_tolerates_the_documented_slack() -> None:
    charges = LookupChargeRepository(
        [_charge(amount_minor=10_000 + HEURISTIC_AMOUNT_SLACK_MINOR)]
    )
    strategy = HeuristicAmountWindowMatch(charges, FrozenClock(NOW))

    outcome = await strategy.match(object(), _item(network_reference=None))

    assert outcome.charge_id == "ch_match"


async def test_heuristic_match_abstains_when_two_charges_are_plausible() -> None:
    """Ambiguous evidence is not evidence.

    Two identical amounts for one merchant on one day is an ordinary Tuesday for a
    subscription business, and picking one of them is how a merchant gets paid for
    somebody else's charge.
    """
    charges = LookupChargeRepository([_charge(charge_id="ch_a"), _charge(charge_id="ch_b")])
    strategy = HeuristicAmountWindowMatch(charges, FrozenClock(NOW))

    outcome = await strategy.match(object(), _item(network_reference=None))

    assert outcome.charge_id is None


async def test_manual_match_resolves_the_charge_an_operator_supplied() -> None:
    charges = LookupChargeRepository([_charge()])

    outcome = await ManualMatch(charges).match(object(), _item(charge_id="ch_match"))

    assert outcome.charge_id == "ch_match"
    assert outcome.method == "manual"


async def test_manual_match_raises_for_a_charge_that_does_not_exist() -> None:
    charges = LookupChargeRepository([])

    with pytest.raises(ChargeProjectionNotFoundError):
        await ManualMatch(charges).match(object(), _item(charge_id="ch_ghost"))


async def test_match_items_stops_at_the_first_hit() -> None:
    charges = LookupChargeRepository([_charge()])
    strategies = [
        ExactReferenceMatch(charges),
        NetworkTransactionMatch(charges),
        HeuristicAmountWindowMatch(charges, FrozenClock(NOW)),
    ]
    item = _item()

    matched = await match_items(object(), [item], strategies, clock=FrozenClock(NOW))

    assert matched == 1
    assert item.match_method == "exact_reference"
    assert charges.calls == ["find_by_processor_reference"]


async def test_match_items_marks_a_heuristic_hit_needs_review() -> None:
    charges = LookupChargeRepository(
        [_charge(processor_reference="X", network_transaction_id="Y")]
    )
    strategies = [
        ExactReferenceMatch(charges),
        NetworkTransactionMatch(charges),
        HeuristicAmountWindowMatch(charges, FrozenClock(NOW)),
    ]
    item = _item(network_reference=None)

    await match_items(object(), [item], strategies, clock=FrozenClock(NOW))

    assert item.status == "needs_review"
    assert item.charge_id == "ch_match"


async def test_match_items_leaves_an_unmatched_line_alone() -> None:
    """No charge, no forced fit. It becomes `orphaned` at posting time instead."""
    charges = LookupChargeRepository([])
    strategies = [ExactReferenceMatch(charges), NetworkTransactionMatch(charges)]
    item = _item()

    matched = await match_items(object(), [item], strategies, clock=FrozenClock(NOW))

    assert matched == 0
    assert item.charge_id is None
    assert item.match_method == "unmatched"
