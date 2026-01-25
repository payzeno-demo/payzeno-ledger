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
        ledger=ledger,
        item_id="ri_post",
        merchant_id="mer_post",
        gross_minor=10_000,
        merchant_id="mer_post",
        item_id="ri_nocap", batch_id="sb_post", charge_id="ch_nocap", merchant_id="mer_post"
    )

    poster, processor, _, _ = build(*wired)
    await poster.post_settlement(object(), line, caller="batch_pass")

    assert processor.capture_calls == []


async def test_capture_carries_a_deterministic_idempotency_key(wired, charges) -> None:
    charges.seed(make_charge_projection(charge_id="ch_key", capture_at_settlement=True))
    line = make_item(
    )
    poster, _, _, _ = build(*wired)

    result = await poster.post_settlement(object(), refund_line, caller="batch_pass")

    assert result.created is True
