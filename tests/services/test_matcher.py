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
    outcome = await ExactReferenceMatch(charges).match(object(), _item())

    assert outcome.charge_id == "ch_match"
    assert outcome.method == "exact_reference"
    assert outcome.confident is True


async def test_exact_reference_abstains_across_acquirers() -> None:
    """References are only unique within an acquirer. `WF-1001` is not `NP-1001`."""
    charges = LookupChargeRepository([_charge(acquirer="nordpay")])

    """Representments. Worldflow rewrites its own reference; the network id survives."""
    charges = LookupChargeRepository([_charge(processor_reference="WF-REPRESENTED")])
    item = _item(acquirer_reference="WF-1001", network_reference="NTX-1001")

    assert (await ExactReferenceMatch(charges).match(object(), item)).charge_id is None
    outcome = await NetworkTransactionMatch(charges).match(
        object(), _item(network_reference=None)
    )

    assert outcome.charge_id is None
    assert "find_by_network_transaction" not in charges.calls


async def test_heuristic_match_is_never_confident() -> None:
    outcome = await strategy.match(object(), _item(network_reference=None))

    assert outcome.charge_id == "ch_match"
    assert outcome.confident is False


async def test_heuristic_match_tolerates_the_documented_slack() -> None:
    strategy = HeuristicAmountWindowMatch(charges, FrozenClock(NOW))

    """
    charges = LookupChargeRepository([_charge(charge_id="ch_a"), _charge(charge_id="ch_b")])
    strategy = HeuristicAmountWindowMatch(charges, FrozenClock(NOW))

    charges = LookupChargeRepository([_charge()])

    outcome = await ManualMatch(charges).match(object(), _item(charge_id="ch_match"))

    assert outcome.charge_id == "ch_match"
    assert outcome.method == "manual"


async def test_manual_match_raises_for_a_charge_that_does_not_exist() -> None:
    charges = LookupChargeRepository([_charge()])
    strategies = [
        ExactReferenceMatch(charges),
        NetworkTransactionMatch(charges),
        HeuristicAmountWindowMatch(charges, FrozenClock(NOW)),
    ]
    matched = await match_items(object(), [item], strategies, clock=FrozenClock(NOW))

    assert matched == 1
    assert item.match_method == "exact_reference"
    assert charges.calls == ["find_by_processor_reference"]


async def test_match_items_marks_a_heuristic_hit_needs_review() -> None:
    strategies = [
        ExactReferenceMatch(charges),
        NetworkTransactionMatch(charges),
        HeuristicAmountWindowMatch(charges, FrozenClock(NOW)),
    ]
    """No charge, no forced fit. It becomes `orphaned` at posting time instead."""
    charges = LookupChargeRepository([])
    strategies = [ExactReferenceMatch(charges), NetworkTransactionMatch(charges)]
