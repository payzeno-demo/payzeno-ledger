"""`RetryScheduler` — app/services/reconciliation/retry.py.

The single most-edited file in this repository, and the one PAY-2041 was written into.

What is asserted here, in order of how much it matters:

1. `_claim_item` asks for the BATCH advisory lock before it takes the row lock, and returns
   None when it cannot have it. Ordering: advisory first, row second, everywhere (ADR 0011).
2. `retry_item` returns `ReconciliationItem | None`. None is a normal outcome — the route
   turns it into 409 settlement_locked and the console shows "already settling".
3. The failure branches run OUTSIDE the business session. This is not a style point: the
   business transaction has already rolled back, so `attempt_count` written inside it is
   gone. Without the out-of-band session the drain would retry the same item forever and
   `RECONCILE_MAX_ATTEMPTS` would be unreachable.

And one thing that is asserted and should not be trusted: `test_retry_is_idempotent` is
sequential. It passed on the buggy code for six months. It is kept exactly as it was, with
this docstring on it, because deleting it would hide the lesson.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.db.locks import AdvisoryLockManager
from app.errors import (
    OrphanedItemError,
    ProcessorUnavailableError,
    RetryableSettlementError,
    RetryExhaustedError,
)
from app.services.reconciliation.constants import MAX_ATTEMPTS, RETRYABLE_STATUSES
from app.services.reconciliation.poster import SettlementPoster
from app.services.reconciliation.retry import RetryScheduler
from app.services.reconciliation.types import SettlementResult
from tests.doubles import CollectingPublisher, FrozenClock, StaticFeatureFlags
from tests.factories import make_item

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 0, 15, tzinfo=UTC)


class FakeLocks(AdvisoryLockManager):
    """Advisory locks without a database. Records every acquisition, in order."""

    def __init__(self, *, grant: bool = True) -> None:
        self.grant = grant
        self.batch_locks: list[str] = []
        self.item_locks: list[str] = []

    reconcile_retry_backoff_base_seconds = 30
    """
    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster)

    poster = StubPoster(
        raises=RetryableSettlementError(item_id="ri_svc", code="processor_unavailable")
    )
    scheduler, _, _ = build(sessions_factory, items, poster)

    await scheduler.retry_item(seeded_item.id)
    first_delay = (seeded_item.next_attempt_at - NOW).total_seconds()
    seeded_item.status = "retryable"

    await scheduler.retry_item(seeded_item.id)
    scheduler = RetryScheduler(
        sessions=sessions_factory,
        items=items,
        poster=poster,
        publisher=CollectingPublisher(),
        clock=FrozenClock(NOW),
        flags=StaticFeatureFlags({}),
        locks=FakeLocks(),
        settings=TightSettings(),
    )
    seeded_item.attempt_count = 1

    assert await scheduler.retry_item(seeded_item.id) is None
    assert seeded_item.last_error_code == "retry_exhausted"
    assert TightSettings.reconcile_max_attempts != MAX_ATTEMPTS


async def test_retry_exhausted_is_the_declared_error_type() -> None:
