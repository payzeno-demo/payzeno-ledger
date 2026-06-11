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
        merchants=merchants,
        ledger=ledger,
        processor=processor,
        publisher=publisher,
        flags=flags or StaticFeatureFlags({"duplicate_settlement_alarm": True}),
        metrics=metrics,
        clock=FrozenClock(NOW),
    )
    return poster, processor, publisher, metrics


@pytest.fixture
def item():
    return make_item(
        item_id="ri_post",
        batch_id="sb_post",
        charge_id="ch_post",
        merchant_id="mer_post",
        status="retryable",
        gross_minor=10_000,
        fee_minor=290,
        net_minor=9_710,
        variance_minor=0,
        line_type="sale",
    )


@pytest.fixture
def wired(transactions, charges, merchants, ledger):
    charges.seed(make_charge_projection(charge_id="ch_post", capture_at_settlement=False))
    merchants.seed(make_merchant_projection(merchant_id="mer_post", settlement_tolerance_minor=100))
    return transactions, charges, merchants, ledger


async def test_post_settlement_returns_a_created_result(wired, item) -> None:
    poster, processor, publisher, _ = build(*wired)

    result = await poster.post_settlement(object(), item, caller="batch_pass")

    assert result.created is True
    assert result.transaction_id
    assert publisher.event_types() == ["settlement.item_settled"]


async def test_confirm_settlement_is_called_unconditionally_and_first(wired, item) -> None:
    poster, processor, _, _ = build(*wired)

    await poster.post_settlement(object(), item, caller="batch_pass")

    assert processor.confirm_calls == [
        {"acquirer": item.acquirer, "acquirer_reference": item.acquirer_reference, "batch_id": item.batch_id}
    ]
    assert processor.call_order[0] == "confirm_settlement"


async def test_confirm_settlement_runs_even_for_a_non_sale_line(wired) -> None:
    fee_line = make_item(
        item_id="ri_fee",
        batch_id="sb_post",
        charge_id="ch_post",
        merchant_id="mer_post",
        gross_minor=413,
        fee_minor=413,
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
        confirm_raises=ProcessorUnavailableError(code="processor_timeout")
    )
    poster, _, _, _ = build(*wired, processor=processor)

    with pytest.raises(Exception) as excinfo:
        await poster.post_settlement(object(), item, caller="retry_scheduler")

    assert not isinstance(excinfo.value, RetryableSettlementError)


async def test_orphan_guard_precedes_the_projection_read(wired) -> None:
    # charge_id is nullable. An unguarded get_or_raise(session, None) raises
    # ChargeProjectionNotFoundError, which is a 404 about a charge that does not exist
    # rather than a 422 about a line we could not match.
    orphan = make_item(
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
        charge_id="ch_deferred",
        merchant_id="mer_post",
        item_id="ri_nocap", batch_id="sb_post", charge_id="ch_nocap", merchant_id="mer_post"
    )

    poster, processor, _, _ = build(*wired)
    await poster.post_settlement(object(), line, caller="batch_pass")

    assert processor.capture_calls == []


async def test_capture_carries_a_deterministic_idempotency_key(wired, charges) -> None:
    charges.seed(make_charge_projection(charge_id="ch_key", capture_at_settlement=True))
    line = make_item(
        item_id="ri_key", batch_id="sb_KEY", charge_id="ch_key", merchant_id="mer_post"
    )
    poster, processor, _, _ = build(*wired)

    await poster.post_settlement(object(), line, caller="batch_pass")

    assert processor.capture_calls[0]["idempotency_key"] == "capture:sb_KEY:ch_key"


async def test_losing_the_race_publishes_duplicate_detected_and_captures_nothing(
    wired, item
) -> None:
    """The behaviour PR #172 bought at 03:05.

    `on_conflict='return_existing'` means the second writer finds out inside one statement
    that it lost, and the `created is False` branch skips the acquirer call entirely. Even a
    lost race cannot reach a cardholder.
    """
    poster, processor, publisher, _ = build(*wired)

    first = await poster.post_settlement(object(), item, caller="batch_pass")
    second = await poster.post_settlement(object(), item, caller="retry_scheduler")

    assert first.created is True
    assert second.created is False
    assert second.transaction_id == first.transaction_id
    assert processor.capture_calls == []
    assert publisher.event_types() == ["settlement.item_settled", "settlement.duplicate_detected"]


async def test_duplicate_detected_names_the_caller_that_lost(wired, item) -> None:
    poster, _, publisher, _ = build(*wired)

    await poster.post_settlement(object(), item, caller="batch_pass")
    await poster.post_settlement(object(), item, caller="retry_scheduler")

    payload = publisher.payload_for("settlement.duplicate_detected")
    assert payload["detected_by"] == "retry_scheduler"
    assert payload["idempotency_key"] == f"settle:{item.batch_id}:{item.id}"
    assert payload["existing_transaction_id"]
    assert payload["merchant_id"] == item.merchant_id


async def test_duplicate_detected_is_published_even_with_the_alarm_flag_off(wired, item) -> None:
    """The flag gates the CloudWatch metric ONLY.

    A flag defaulted off in front of the publish would silently disable the very alarm
    (PAY-2055) the incident exists to produce.
    """
    poster, _, publisher, metrics = build(
        *wired, flags=StaticFeatureFlags({"duplicate_settlement_alarm": False})
    )

    await poster.post_settlement(object(), item, caller="batch_pass")
    await poster.post_settlement(object(), item, caller="retry_scheduler")

    assert "settlement.duplicate_detected" in publisher.event_types()
    assert metrics.increments == []


async def test_the_alarm_metric_fires_when_the_flag_is_on(wired, item) -> None:
    poster, _, _, metrics = build(*wired)

    await poster.post_settlement(object(), item, caller="batch_pass")
    await poster.post_settlement(object(), item, caller="retry_scheduler")

    assert metrics.increments[0][0] == "DuplicateSettlementDetected"
    assert metrics.increments[0][1]["acquirer"] == item.acquirer


async def test_the_idempotency_key_subject_is_the_item(wired, item, ledger) -> None:
    poster, _, _, _ = build(*wired)

    await poster.post_settlement(object(), item, caller="batch_pass")

    assert ledger.posted[0]["idempotency_key"] == f"settle:{item.batch_id}:{item.id}"
    assert ledger.posted[0]["reference_type"] == "reconciliation_item"
    assert ledger.posted[0]["reference_id"] == item.id


async def test_the_ledger_post_asks_for_return_existing(wired, item, ledger) -> None:
    poster, _, _, _ = build(*wired)

    await poster.post_settlement(object(), item, caller="batch_pass")

    assert ledger.posted[0]["on_conflict"] == "return_existing"
    assert ledger.posted[0]["created_by"] == "reconciliation"
    assert ledger.posted[0]["purpose"] == "settle"


async def test_the_publisher_is_the_outbox(wired, item) -> None:
    # A rolled-back attempt must emit nothing. During the outage a direct SNS publisher
    # would have emitted thousands of phantom settlement.item_settled events for items that
    # never settled — and PAY-2055's alarm counts exactly two events per duplicated charge.
    from app.publishers.outbox import OutboxPublisher

    import inspect

    signature = inspect.signature(SettlementPoster.__init__)
    annotation = signature.parameters["publisher"].annotation
    assert annotation in (OutboxPublisher, "OutboxPublisher")


async def test_line_type_selects_the_rule(wired) -> None:
    refund_line = make_item(
        item_id="ri_refund",
        batch_id="sb_post",
        charge_id="ch_post",
        merchant_id="mer_post",
        line_type="refund",
        gross_minor=4_000,
        net_minor=4_000,
    )
    poster, _, _, _ = build(*wired)

    result = await poster.post_settlement(object(), refund_line, caller="batch_pass")

    assert result.created is True
