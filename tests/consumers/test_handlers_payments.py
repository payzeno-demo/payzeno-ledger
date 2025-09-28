"""Payment event handlers — app/consumers/handlers/payments.py.

Every projection write here is a conditional upsert guarded on `source_occurred_at`. The
bus does not guarantee ordering, and a stale `payment.authorized` replay that overwrote a
newer row would silently flip `capture_at_settlement` back — which is the field that
decides whether the ledger issues a second cardholder capture. That is not a hypothetical
severity; it is the same field that turned 1,847 ledger duplicates into 218 real ones.

`handle_dispute_opened` is the one handler that raises. `DuplicateDisputeError` is a real
state — the acquirer files the same dispute twice about once a month — and it has to be
distinguishable from a redelivery, which `_claim_event` already ate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.consumers.handlers.payments import (
    handle_dispute_closed,
    handle_dispute_opened,
    handle_payment_authorized,
    handle_payment_canceled,
    handle_payment_captured,
    handle_refund_created,
)
from app.errors import DuplicateDisputeError
from app.repositories.projections import SettlementChargeRepository
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=2)


class UpsertingCharges(SettlementChargeRepository):
    """`upsert_if_newer` against a dict, with the real ordering guard."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, Any] = {}
        self.rejected: list[str] = []

    async def upsert_if_newer(self, session: Any, projection: Any) -> bool:
        existing = self.rows.get(projection.charge_id)
        if existing is not None and existing.source_occurred_at >= projection.source_occurred_at:
            self.rejected.append(projection.charge_id)
            return False
        self.rows[projection.charge_id] = projection
        return True

    async def get_or_raise(self, session: Any, charge_id: str) -> Any:
        return self.rows[charge_id]


class RecordingLedger:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self._seq = 0

    async def post(self, session: Any, **kwargs: Any) -> Any:
        self._seq += 1
        self.posts.append(kwargs)
        transaction = type("Txn", (), {"id": f"txn_h_{self._seq}"})()
        return type("PostResult", (), {"transaction": transaction, "created": True})()

    def purposes(self) -> list[str]:
        return [post["purpose"] for post in self.posts]


class LookupTransactions:
    def __init__(self, by_key: dict[str, Any] | None = None) -> None:
        self.by_key = by_key or {}

    async def find_by_idempotency_key(self, session: Any, key: str) -> Any | None:
        return self.by_key.get(key)


def _authorized_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "charge_id": "ch_h1",
        "merchant_id": "mer_h",
        "amount_minor": 10_000,
        "currency": "USD",
        "acquirer": "worldflow",
        "network_transaction_id": "NTX-h1",
        "processor_reference": "WF-h1",
        "capture_method": "automatic",
        "capture_at_settlement": True,
        "reserve_bps": 1_000,
        "platform_fee_bps": 290,
        "platform_fee_fixed_minor": 30,
        "authorized_at": NOW.isoformat(),
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------------------
# payment.authorized
# --------------------------------------------------------------------------------------


async def test_authorized_projects_the_charge_and_posts_the_auth() -> None:
    charges = UpsertingCharges()
    ledger = RecordingLedger()

    await handle_payment_authorized(
        object(),
        _authorized_payload(),
        charges=charges,
        ledger=ledger,
        clock=FrozenClock(NOW),
        event_id="evt_h1",
        occurred_at=NOW,
        livemode=True,
    )

    assert "ch_h1" in charges.rows
    assert ledger.purposes() == ["auth"]


async def test_the_rate_fields_are_denormalised_onto_the_projection() -> None:
    """Frozen at authorisation time, on purpose.

    A merchant renegotiating their rate on Tuesday must not retroactively change what an
    in-flight settlement from Monday books. `SettlementPoster` reads these off the charge
    row, never off the merchant row.
    """
    charges = UpsertingCharges()

    await handle_payment_authorized(
        object(),
        _authorized_payload(reserve_bps=1_500, platform_fee_bps=310),
        charges=charges,
        ledger=RecordingLedger(),
        clock=FrozenClock(NOW),
        event_id="evt_h1",
        occurred_at=NOW,
        livemode=True,
    )

    projection = charges.rows["ch_h1"]
    assert projection.reserve_bps == 1_500
    assert projection.platform_fee_bps == 310
    assert projection.capture_at_settlement is True


async def test_a_stale_replay_does_not_overwrite_a_newer_projection() -> None:
    """The ordering guard, and the reason it exists.

    An out-of-order replay flipping `capture_at_settlement` back to true is a second
    cardholder capture with no ledger duplicate to notice it by.
    """
    charges = UpsertingCharges()
    ledger = RecordingLedger()

    await handle_payment_authorized(
        object(),
        _authorized_payload(capture_at_settlement=False),
        charges=charges,
        ledger=ledger,
        clock=FrozenClock(NOW),
        event_id="evt_new",
        occurred_at=NOW,
        livemode=True,
    )
    await handle_payment_authorized(
        object(),
        _authorized_payload(capture_at_settlement=True),
        charges=charges,
        ledger=ledger,
        clock=FrozenClock(NOW),
        event_id="evt_stale",
        occurred_at=EARLIER,
        livemode=True,
    )

    assert charges.rows["ch_h1"].capture_at_settlement is False
    assert charges.rejected == ["ch_h1"]
    assert ledger.purposes() == ["auth"], "the stale replay posted a second auth"


async def test_the_auth_posting_is_idempotent_by_construction() -> None:
    """`on_conflict='return_existing'`. A redelivery that beats `_claim_event` is fine."""
    ledger = RecordingLedger()

    await handle_payment_authorized(
        object(),
        _authorized_payload(),
        charges=UpsertingCharges(),
        ledger=ledger,
        clock=FrozenClock(NOW),
        event_id="evt_h1",
        occurred_at=NOW,
        livemode=True,
    )

    assert ledger.posts[0]["on_conflict"] == "return_existing"


# --------------------------------------------------------------------------------------
# payment.captured / canceled / refund.created
# --------------------------------------------------------------------------------------


async def test_captured_stamps_the_charge_and_posts_the_capture_legs() -> None:
    charges = UpsertingCharges()
    ledger = RecordingLedger()
    await handle_payment_authorized(
        object(),
        _authorized_payload(),
        charges=charges,
        ledger=ledger,
        clock=FrozenClock(NOW),
        event_id="evt_h1",
        occurred_at=EARLIER,
        livemode=True,
    )

    await handle_payment_captured(
        object(),
        {
            "charge_id": "ch_h1",
            "merchant_id": "mer_h",
            "currency": "USD",
            "captured_amount_minor": 10_000,
            "captured_at": NOW.isoformat(),
        },
        charges=charges,
        ledger=ledger,
        clock=FrozenClock(NOW),
        event_id="evt_h2",
        occurred_at=NOW,
        livemode=True,
    )

    assert charges.rows["ch_h1"].captured_at == NOW
    assert ledger.purposes() == ["auth", "capture"]


async def test_canceled_without_a_charge_posts_nothing() -> None:
    """An intent cancelled before any charge existed has nothing to release."""
    ledger = RecordingLedger()

    await handle_payment_canceled(
        object(),
        {"merchant_id": "mer_h", "currency": "USD", "released_amount_minor": 0},
        ledger=ledger,
        event_id="evt_h3",
        livemode=True,
    )

    assert ledger.posts == []


async def test_canceled_with_a_charge_releases_the_authorisation() -> None:
    ledger = RecordingLedger()

    await handle_payment_canceled(
        object(),
        {
            "charge_id": "ch_h1",
            "merchant_id": "mer_h",
            "currency": "USD",
            "released_amount_minor": 10_000,
        },
        ledger=ledger,
        event_id="evt_h3",
        livemode=True,
    )

    assert ledger.purposes() == ["auth_release"]


async def test_a_refund_that_nets_against_settlement_posts_nothing_here() -> None:
    """The acquirer will file a `refund` line and reconciliation will handle it.

    Posting here as well is how a refund gets booked twice — once against the charge and
    once against the settlement line — which is a different double-count with the same
    shape as PAY-2041 and no unique key to stop it either.
    """
    ledger = RecordingLedger()

    await handle_refund_created(
        object(),
        {
            "refund_id": "re_h1",
            "charge_id": "ch_h1",
            "merchant_id": "mer_h",
            "currency": "USD",
            "amount_minor": 2_500,
            "nets_against_settlement": True,
        },
        ledger=ledger,
        event_id="evt_h4",
        livemode=True,
    )

    assert ledger.posts == []


async def test_a_standalone_refund_posts() -> None:
    ledger = RecordingLedger()

    await handle_refund_created(
        object(),
        {
            "refund_id": "re_h2",
            "charge_id": "ch_h1",
            "merchant_id": "mer_h",
            "currency": "USD",
            "amount_minor": 2_500,
            "nets_against_settlement": False,
        },
        ledger=ledger,
        event_id="evt_h5",
        livemode=True,
    )

    assert ledger.purposes() == ["refund"]
    assert ledger.posts[0]["reference_type"] == "refund"


# --------------------------------------------------------------------------------------
# disputes
# --------------------------------------------------------------------------------------


async def test_dispute_opened_posts_the_liability_move() -> None:
    ledger = RecordingLedger()

    await handle_dispute_opened(
        object(),
        {
            "dispute_id": "dp_h1",
            "charge_id": "ch_h1",
            "merchant_id": "mer_h",
            "currency": "USD",
            "amount_minor": 4_000,
            "fee_minor": 1_500,
        },
        ledger=ledger,
        transactions=LookupTransactions(),
        event_id="evt_h6",
        livemode=True,
    )

    assert ledger.purposes() == ["dispute"]


async def test_a_dispute_filed_twice_raises() -> None:
    """The acquirer does this about once a month. It is a state, not a bug.

    It has to be distinguishable from a redelivery, which `_claim_event` already
    swallowed one layer up.
    """
    existing = type("Txn", (), {"id": "txn_existing"})()
    transactions = LookupTransactions({"dispute:mer_h:dp_h1": existing})

    with pytest.raises(DuplicateDisputeError):
        await handle_dispute_opened(
            object(),
            {
                "dispute_id": "dp_h1",
                "charge_id": "ch_h1",
                "merchant_id": "mer_h",
                "currency": "USD",
                "amount_minor": 4_000,
                "fee_minor": 1_500,
            },
            ledger=RecordingLedger(),
            transactions=transactions,
            event_id="evt_h7",
            livemode=True,
        )


async def test_a_lost_dispute_reverses_nothing() -> None:
    """Losing means the money stays gone. Reversing on `closed` refunds every loss."""
    ledger = RecordingLedger()

    await handle_dispute_closed(
        object(),
        {
            "dispute_id": "dp_h1",
            "merchant_id": "mer_h",
            "currency": "USD",
            "amount_minor": 4_000,
            "outcome": "lost",
        },
        ledger=ledger,
        event_id="evt_h8",
        livemode=True,
    )

    assert ledger.posts == []


async def test_a_won_dispute_reverses_the_liability() -> None:
    ledger = RecordingLedger()

    await handle_dispute_closed(
        object(),
        {
            "dispute_id": "dp_h1",
            "merchant_id": "mer_h",
            "currency": "USD",
            "amount_minor": 4_000,
            "outcome": "won",
        },
        ledger=ledger,
        event_id="evt_h9",
        livemode=True,
    )

    assert ledger.purposes() == ["dispute_reversal"]
