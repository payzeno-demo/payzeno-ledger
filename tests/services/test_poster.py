"""`SettlementPoster` — app/services/reconciliation/poster.py.

The only writer of `purpose='settle'` transactions, and the shared call site: one instance
serves both `ReconciliationService` and `RetryScheduler`. That sharing is why `session` and
`caller` are per-call arguments and not constructor state.

Since PR #172 the guard is an atomic upsert — `LedgerPoster.post(..., on_conflict=
'return_existing')` — and `capture_deferred` is issued only when `created` is true. The
check-then-act version this replaced is described in the postmortem; what is asserted below
is the shape that came out of it:

    confirm_settlement (unconditional, first)
      -> orphan guard
      -> claim + post
      -> if not created: publish settlement.duplicate_detected, NO capture, return
      -> if created and charge.capture_at_settlement: capture_deferred
      -> publish settlement.item_settled

`confirm_settlement` runs for every item whatever its line_type and whoever captures. It is
what failed for all 4,113 items during the Worldflow degradation; without it only the eleven
deferred-capture merchants could ever have gone retryable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.errors import (
    ChargeProjectionNotFoundError,
    OrphanedItemError,
    ProcessorUnavailableError,
    RetryableSettlementError,
    SettlementVarianceExceededError,
)
from app.services.reconciliation.constants import RETRYABLE_ERROR_CODES
from app.services.reconciliation.poster import SettlementPoster
from tests.doubles import CollectingPublisher, FrozenClock, RecordingProcessorClient, StaticFeatureFlags
from tests.factories import make_charge_projection, make_item, make_merchant_projection

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 0, 15, tzinfo=UTC)


class CountingMetrics:
    def __init__(self) -> None:
        self.increments: list[tuple[str, dict[str, Any]]] = []

    def increment(self, name: str, **labels: Any) -> None:
        self.increments.append((name, labels))


def build(transactions, charges, merchants, ledger, *, processor=None, flags=None):
    processor = processor or RecordingProcessorClient()
    publisher = CollectingPublisher()
    metrics = CountingMetrics()
    poster = SettlementPoster(
        transactions=transactions,
        charges=charges,
        ledger=ledger,
        flags=flags or StaticFeatureFlags({"duplicate_settlement_alarm": True}),
        clock=FrozenClock(NOW),
    )
    return poster, processor, publisher, metrics


@pytest.fixture
def item():
    return make_item(
        item_id="ri_post",
        batch_id="sb_post",
        merchant_id="mer_post",
        gross_minor=10_000,
        fee_minor=290,
        net_minor=9_710,
        variance_minor=0,
        batch_id="sb_post",
        merchant_id="mer_post",
        gross_minor=413,
        net_minor=0,
    )
    poster, processor, _, _ = build(*wired)

    await poster.post_settlement(object(), fee_line, caller="batch_pass")

    assert processor.confirm_calls


async def test_confirm_settlement_failure_becomes_a_retryable_error(wired, item) -> None:
    """The 22 minutes of Worldflow 504s, in one assertion.

    Every item confirms its line. Every confirm returned 504. So every item went retryable —
    4,113 of them across two batches — and the drain had a backlog large enough for three
    sweeps to land inside it.
    """
    processor = RecordingProcessorClient(
        confirm_raises=ProcessorUnavailableError(code="processor_unavailable")
    )
    poster, _, _, _ = build(*wired, processor=processor)

    with pytest.raises(RetryableSettlementError) as excinfo:
        await poster.post_settlement(object(), item, caller="retry_scheduler")

    assert excinfo.value.code == "processor_unavailable"
    assert excinfo.value.code in RETRYABLE_ERROR_CODES


async def test_an_indeterminate_code_is_not_retried_blindly(wired, item) -> None:
    # PAY-2060. A timeout on a capture is the one state where we do not know whether the
    # cardholder was charged, so it must not go down the plain retry path.
    processor = RecordingProcessorClient(
        item_id="ri_orphan", batch_id="sb_post", charge_id=None, merchant_id="mer_post"
    )
    poster, _, _, _ = build(*wired)

    with pytest.raises(OrphanedItemError) as excinfo:
        await poster.post_settlement(object(), orphan, caller="batch_pass")

    assert not isinstance(excinfo.value, ChargeProjectionNotFoundError)
    assert excinfo.value.details["item_id"] == "ri_orphan"


async def test_variance_beyond_tolerance_posts_nothing(wired, item, ledger) -> None:
    item.variance_minor = -5_000

    poster, _, publisher, _ = build(*wired)

    with pytest.raises(SettlementVarianceExceededError):
        await poster.post_settlement(object(), item, caller="batch_pass")

    assert ledger.posted == []
    assert "settlement.item_settled" not in publisher.event_types()


async def test_variance_within_tolerance_posts_normally(wired, item) -> None:
    item.variance_minor = 40  # tolerance is 100 minor units

    poster, _, _, _ = build(*wired)
    result = await poster.post_settlement(object(), item, caller="batch_pass")

    assert result.created is True


async def test_capture_deferred_only_for_capture_at_settlement_charges(
    wired, item, charges
) -> None:
    poster, processor, _, _ = build(*wired)
    await poster.post_settlement(object(), item, caller="batch_pass")
    assert processor.capture_calls == []

    charges.seed(make_charge_projection(charge_id="ch_deferred", capture_at_settlement=True))
    deferred = make_item(
        item_id="ri_deferred",
        batch_id="sb_post",
        merchant_id="mer_post",
        item_id="ri_nocap", batch_id="sb_post", charge_id="ch_nocap", merchant_id="mer_post"
    )

    poster, processor, _, _ = build(*wired)
    await poster.post_settlement(object(), line, caller="batch_pass")

    assert processor.capture_calls == []


async def test_capture_carries_a_deterministic_idempotency_key(wired, charges) -> None:
    charges.seed(make_charge_projection(charge_id="ch_key", capture_at_settlement=True))
    line = make_item(
        item_id="ri_refund",
        charge_id="ch_post",
        net_minor=4_000,
    )
    poster, _, _, _ = build(*wired)

    result = await poster.post_settlement(object(), refund_line, caller="batch_pass")

    assert result.created is True
