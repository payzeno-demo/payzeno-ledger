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

    async def acquire_batch_lock(self, session: Any, batch_id: str) -> None:
        self.batch_locks.append(batch_id)

    async def acquire_item_lock(self, session: Any, item_id: str) -> None:
        self.item_locks.append(item_id)


class StubPoster(SettlementPoster):
    """`post_settlement` with the ledger write replaced by a counter."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.raises = raises
        self._seq = 0
        self.settled_by_key: dict[str, str] = {}

    async def post_settlement(self, session: Any, item: Any, *, caller: str) -> SettlementResult:
        self.calls.append((item.id, caller))
        if self.raises is not None:
            raise self.raises

        key = f"settle:{item.batch_id}:{item.id}"
        if key in self.settled_by_key:
            return SettlementResult(transaction_id=self.settled_by_key[key], created=False)

        self._seq += 1
        transaction_id = f"txn_{self._seq:04d}"
        self.settled_by_key[key] = transaction_id
        return SettlementResult(transaction_id=transaction_id, created=True)


class Settings:
    reconcile_retry_backoff_base_seconds = 30
    retry_drain_batch_size = 50


def build(
    sessions_factory,
    items,
    poster: StubPoster,
    *,
    locks: FakeLocks | None = None,
) -> tuple[RetryScheduler, CollectingPublisher, FakeLocks]:
    publisher = CollectingPublisher()
    lock_manager = locks or FakeLocks()
    claimed = await scheduler._claim_item(sessions_factory.session, seeded_item.id)

    assert claimed is not None
    assert locks.batch_locks == [seeded_item.batch_id]
    # Never the item lock. That one belongs to PayoutService, keyed on payout_id.
    assert locks.item_locks == []


async def test_claim_item_returns_none_when_a_sweep_holds_the_batch_lock(
    sessions_factory, items, seeded_item
) -> None:
    """The fix, stated as behaviour.

    A retry that loses the batch lock is a no-op, not a failure. The item stays retryable,
    the sweep settles it, and the next drain's `WHERE status IN RETRYABLE_STATUSES` no
    longer matches it.
    """
    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster, locks=FakeLocks(grant=False))

    assert await scheduler._claim_item(sessions_factory.session, seeded_item.id) is None
    assert poster.calls == []


async def test_claim_item_returns_none_for_an_unknown_item(sessions_factory, items) -> None:
    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster)

    assert await scheduler._claim_item(sessions_factory.session, "ri_ghost") is None


async def test_claim_item_ignores_items_outside_retryable_statuses(
    sessions_factory, items, batches
) -> None:
    from tests.factories import make_batch

    batches.seed(make_batch(batch_id="sb_done", status="reconciled"))
    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster)

    assert "settled" not in RETRYABLE_STATUSES
    assert await scheduler._claim_item(sessions_factory.session, settled.id) is None


# --------------------------------------------------------------------------------------
# retry_item
# --------------------------------------------------------------------------------------


async def test_retry_item_settles_and_returns_the_item(sessions_factory, items, seeded_item) -> None:
    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster)

    result = await scheduler.retry_item(seeded_item.id, requested_by="ops:noa")

    assert result is not None
    assert result.status == "settled"
    assert result.settled_transaction_id == "txn_0001"
    assert result.attempt_count == 1
    assert result.last_attempt_at == NOW
    assert poster.calls == [(seeded_item.id, "retry_scheduler")]


async def test_retry_item_passes_its_own_caller_tag(sessions_factory, items, seeded_item) -> None:
    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster, locks=FakeLocks(grant=False))

    assert await scheduler.retry_item(seeded_item.id) is None


async def test_retry_is_idempotent(sessions_factory, items, seeded_item) -> None:
    """PAY-1607's original test. It passes on the buggy code and it always did.

    Two calls, one after the other, one event loop, one session. The second call's SELECT
    sees the first call's committed row, so of course only one transaction exists. Nothing
    here runs the sweep and the retry at the same time, and the shared in-process session
    fixture makes it impossible to. That gap is PAY-2053, and the test that closes it is
    tests/integration/test_reconciliation_concurrency.py.
    """
    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster)

    first = await scheduler.retry_item(seeded_item.id)
    second = await scheduler.retry_item(seeded_item.id)

    assert first is not None
    assert second is None  # already settled -> not claimable
    assert len(set(poster.settled_by_key.values())) == 1


# --------------------------------------------------------------------------------------
# the out-of-band failure branches
# --------------------------------------------------------------------------------------


async def test_failure_persists_attempt_count(sessions_factory, items, seeded_item) -> None:
    """The branch that makes the incident's 4,113-item backlog possible at all.

    `post_settlement` raises, the business transaction rolls back, and everything written
    inside it — attempt_count, last_attempt_at, status='settling' — is gone. `_mark_retryable`
    therefore opens its OWN session and writes them again. If it did not, the drain could
    never mark anything retryable and would spin on the same item forever.
    before = sessions_factory.begin_count

    result = await scheduler.retry_item(seeded_item.id)

    assert result is None
    assert seeded_item.status == "retryable"
    assert seeded_item.attempt_count == 1
    assert seeded_item.last_error_code == "processor_unavailable"
    assert seeded_item.next_attempt_at > NOW
    # a second `begin()` after the business one — the out-of-band transaction
    assert sessions_factory.begin_count >= before + 2


async def test_retryable_error_code_reaches_the_item(sessions_factory, items, seeded_item) -> None:
    poster = StubPoster(raises=RetryableSettlementError(item_id="ri_svc", code="rate_limited"))
    scheduler, _, _ = build(sessions_factory, items, poster)

    await scheduler.retry_item(seeded_item.id)

    assert seeded_item.last_error_code == "rate_limited"


async def test_backoff_grows_with_attempt_count(sessions_factory, items, seeded_item) -> None:
    poster = StubPoster(
        raises=RetryableSettlementError(item_id="ri_svc", code="processor_unavailable")
    )
    scheduler, _, _ = build(sessions_factory, items, poster)

    await scheduler.retry_item(seeded_item.id)
    first_delay = (seeded_item.next_attempt_at - NOW).total_seconds()
    seeded_item.status = "retryable"

    await scheduler.retry_item(seeded_item.id)
    poster = StubPoster(raises=OrphanedItemError(item_id="ri_svc"))
    scheduler, _, _ = build(sessions_factory, items, poster)

    assert await scheduler.retry_item(seeded_item.id) is None
    assert seeded_item.status == "failed"
    assert seeded_item.last_error_code == "orphaned_item"


async def test_attempts_are_exhausted_at_the_configured_maximum(
    sessions_factory, items, seeded_item
) -> None:
    seeded_item.attempt_count = Settings.reconcile_max_attempts
    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster)

    assert await scheduler.retry_item(seeded_item.id) is None
    assert seeded_item.status == "failed"
    assert seeded_item.last_error_code == "retry_exhausted"
    assert poster.calls == []


async def test_max_attempts_comes_from_settings_not_the_module_constant(
    sessions_factory, items, seeded_item
) -> None:
    """RECONCILE_MAX_ATTEMPTS -> Settings.reconcile_max_attempts is the read path.

    `constants.MAX_ATTEMPTS` is only the default value baked into the module. Reading the
    constant directly would make the env var dead and the knob unturnable at 01:44.
    """

    poster = StubPoster()
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
    # ProcessorUnavailableError is what the client raises; SettlementPoster translates it to
    # RetryableSettlementError when the code is in RETRYABLE_ERROR_CODES. If an untranslated
    # one leaks through, it is still a PayzenoLedgerError and the item fails rather than
    # spinning — which is the safe direction.
    poster = StubPoster(raises=ProcessorUnavailableError(code="processor_unavailable"))
    scheduler, _, _ = build(sessions_factory, items, poster)

    assert await scheduler.retry_item(seeded_item.id) is None
    assert seeded_item.status == "failed"


# --------------------------------------------------------------------------------------
# drain
# --------------------------------------------------------------------------------------


async def test_drain_processes_every_due_item(sessions_factory, items, batches) -> None:
    from tests.factories import make_batch

    batches.seed(make_batch(batch_id="sb_drain", status="closed"))
    for n in range(5):
        items.seed(
            make_item(
                item_id=f"ri_drain_{n}",
                batch_id="sb_drain",
                charge_id="ch_default",
                merchant_id="mer_default",
                status="retryable",
                next_attempt_at=NOW,
            )
        )

    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster)

    settled = await scheduler.drain(limit=50)

    assert settled == 5
    assert len(poster.calls) == 5


async def test_drain_respects_its_limit(sessions_factory, items, batches) -> None:
    from tests.factories import make_batch

    batches.seed(make_batch(batch_id="sb_drain_limit", status="closed"))
    for n in range(10):
        items.seed(
            make_item(
                item_id=f"ri_lim_{n:02d}",
                batch_id="sb_drain_limit",
                charge_id="ch_default",
                merchant_id="mer_default",
                status="retryable",
                next_attempt_at=NOW,
            )
        )

    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster)

    assert await scheduler.drain(limit=3) == 3


async def test_drain_counts_only_the_items_it_actually_settled(
    sessions_factory, items, batches
) -> None:
    """mregression's objection on PR #171, as a test.

    During a sweep every claim in the pass fails to acquire the batch lock and returns None,
    so drain throughput for that batch collapses to zero for as long as the sweep runs. That
    is accepted — the sweep is settling the same items anyway — and PAY-2057 is the ticket
    to stop discovering it lock by lock.
    """
    from tests.factories import make_batch

    batches.seed(make_batch(batch_id="sb_swept", status="closed"))
    for n in range(4):
        items.seed(
            make_item(
                item_id=f"ri_swept_{n}",
                batch_id="sb_swept",
                charge_id="ch_default",
                merchant_id="mer_default",
                status="retryable",
                next_attempt_at=NOW,
            )
        )

    poster = StubPoster()
    scheduler, _, _ = build(sessions_factory, items, poster, locks=FakeLocks(grant=False))

    assert await scheduler.drain(limit=200) == 0
    assert poster.calls == []


async def test_drain_of_an_empty_backlog_is_zero(sessions_factory, items) -> None:
