"""PAY-2053 — the regression test for PAY-2041.

This is the test that did not exist for nine months. Every unit test in
`tests/services/` was written against a single shared in-process session, which makes a
sweep and a retry indistinguishable from two sequential calls, and both of them pass on
the buggy code. See `tests/services/test_retry.py::test_retry_is_idempotent` for the one
that looked thorough.

What is asserted:

* one `purpose='settle'` transaction for the item, not two
* one `capture_deferred` call, not two — this is the assertion that maps onto a
  cardholder statement
* `retry_item` returns `None`, specifically. `reconcile_batch` returns a
  `ReconciliationRun` and can never be `None`, so `any(r is None for r in results)`
  would be satisfied by accident rather than by the property under test

Fails on 1.31.1 (two settle transactions, two captures).
Passes on 1.31.2 (one settle transaction, one capture, retry returns None).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any, Awaitable, TypeVar

import pytest

from app.db.locks import AdvisoryLockManager
from app.ports import CaptureResponse, CaptureStatus, ProcessorClient
from app.publishers.outbox import OutboxPublisher
from app.repositories.ledger_transaction import LedgerTransactionRepository
from app.repositories.account import AccountRepository
from app.repositories.balance_cache import MerchantBalanceCacheRepository
from app.repositories.ledger_entry import LedgerEntryRepository
from app.services.accounts import AccountResolver
from app.repositories.outbox import EventOutboxRepository
from app.repositories.projections import MerchantProjectionRepository
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.reconciliation_run import ReconciliationRunRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.repositories.settlement_charge import SettlementChargeRepository
from app.services.reconciliation.poster import SettlementPoster
from app.services.reconciliation.reconciler import ReconciliationService
from app.services.reconciliation.retry import RetryScheduler
from app.services.transactions import LedgerPoster
from tests.doubles import FrozenClock, StaticFeatureFlags

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 1, 22, 0, 15, tzinfo=UTC)

T = TypeVar("T")


class RecordingProcessorClient(ProcessorClient):
    """Test double that also SYNCHRONISES the two callers.

    An `asyncio.Barrier` at the top of `capture_deferred` means neither caller can commit
    until both have inserted their `ledger_transaction` row. Without it the test is
    timing-dependent: a plain barrier at coroutine start only synchronises the START, and
    the sweep then does `_runs.start`, acquires the advisory lock, opens a read session
    and lists items — several round trips — while the retry goes straight to
    `_claim_item` and `post_settlement`.

    `latency_ms=400` is not scaffolding. With the barrier in place `latency_ms=0` also
    fails on the buggy code, but the barrier is test-only and the latency is what holds
    the window open in production: `capture_deferred` is a 200-900ms external call sitting
    between the INSERT and the COMMIT.
    """

    def __init__(self, *, latency_ms: int, parties: int = 2) -> None:
        self.capture_calls: list[dict[str, Any]] = []
        self.confirm_calls: list[dict[str, Any]] = []
        self._latency_ms = latency_ms
        self._gate = asyncio.Barrier(parties)

    async def confirm_settlement(
        self, acquirer: str, acquirer_reference: str, batch_id: str
    ) -> None:
        self.confirm_calls.append(
            {
                "acquirer": acquirer,
                "acquirer_reference": acquirer_reference,
                "batch_id": batch_id,
            }
        )

    async def capture_deferred(
        self,
        charge_id: str,
        amount_minor: int,
        currency: str,
        reference: str,
        *,
        idempotency_key: str,
    ) -> CaptureResponse:
        self.capture_calls.append(
            {
                "charge_id": charge_id,
                "amount_minor": amount_minor,
                "currency": currency,
                "idempotency_key": idempotency_key,
            }
        )
        await self._gate.wait()
        await asyncio.sleep(self._latency_ms / 1000)
        return CaptureResponse(
            captured=True, reference=idempotency_key, captured_at=NOW
        )

    async def get_capture_status(
        self, acquirer: str, idempotency_key: str
    ) -> CaptureStatus:
        for call in self.capture_calls:
            if call["idempotency_key"] == idempotency_key:
                return CaptureStatus(state="captured", reference=idempotency_key)
        return CaptureStatus(state="not_captured", reference=None)

    async def fetch_settlement_file(self, acquirer: str, processing_date: date) -> bytes:
        return b""


class _Settings:
    """Only the fields the two services read. Real `Settings` needs a real environment."""

    reconcile_max_attempts = 5
    reconcile_max_items_per_run = 500
    reconcile_sweep_wall_budget_seconds = 30
    reconcile_retry_backoff_base_seconds = 30
    retry_drain_batch_size = 50


def _build(sessions, processor: RecordingProcessorClient):
    """Wire the real graph. Only the acquirer is a double.

    Both services get the SAME `SettlementPoster` instance and the same repository
    instances — that sharing is the hazard, and a test that gives each path its own
    collaborators is testing something else.
    """
    transactions = LedgerTransactionRepository()
    entries = LedgerEntryRepository()
    accounts = AccountRepository()
    balances = MerchantBalanceCacheRepository()
    items = ReconciliationItemRepository()
    batches = SettlementBatchRepository()
    runs = ReconciliationRunRepository()
    charges = SettlementChargeRepository()
    merchants = MerchantProjectionRepository()
    publisher = OutboxPublisher(EventOutboxRepository())
    clock = FrozenClock(NOW)
    locks = AdvisoryLockManager()
    settings = _Settings()

    ledger = LedgerPoster(
        transactions=transactions,
        entries=entries,
        accounts=accounts,
        balances=balances,
        resolver=AccountResolver(accounts, clock),
        publisher=publisher,
        clock=clock,
    )
    poster = SettlementPoster(
        transactions=transactions,
        charges=charges,
        merchants=merchants,
        ledger=ledger,
        processor=processor,
        publisher=publisher,
        flags=StaticFeatureFlags({"duplicate_settlement_alarm": True}),
        clock=clock,
    )
    sweep = ReconciliationService(
        sessions=sessions,
        locks=locks,
        batches=batches,
        items=items,
        runs=runs,
        poster=poster,
        publisher=publisher,
        clock=clock,
        settings=settings,
    )
    retry = RetryScheduler(
        sessions=sessions,
        items=items,
        poster=poster,
        publisher=publisher,
        clock=clock,
        flags=StaticFeatureFlags({"reconcile_batch_lock_on_retry": True}),
        locks=locks,
        settings=settings,
    )
    return sweep, retry


async def _with_barrier(gate: asyncio.Barrier, coro: Awaitable[T]) -> T:
    """Start both coroutines at the same point in the event loop.

    Necessary but nowhere near sufficient — see `RecordingProcessorClient`'s docstring.
    """
    await gate.wait()
    return await coro


async def test_sweep_and_retry_cannot_double_post(
    sessions, seeded_batch, count_transactions, count_entries
) -> None:
    """Regression for PAY-2041."""
    item_id = seeded_batch.first_item_id
    processor = RecordingProcessorClient(latency_ms=400)
    sweep, retry = _build(sessions, processor)

    start = asyncio.Barrier(2)
    run, retried = await asyncio.gather(
        _with_barrier(start, sweep.reconcile_batch(seeded_batch.id, trigger="manual")),
        _with_barrier(start, retry.retry_item(item_id, requested_by="test")),
    )

    txns = await count_transactions(purpose="settle", reference_id=item_id)
    assert txns == 1
    assert len(processor.capture_calls) == 1
    # The retry, specifically. reconcile_batch returns a ReconciliationRun and can never
    # be None, so the loose `any(... is None ...)` form would pass for the wrong reason.
    assert retried is None
    assert run.items_settled >= 1


async def test_double_post_writes_double_entries_when_it_happens(
    sessions, seeded_batch, count_entries
) -> None:
    """The observable shape of the damage, asserted so the fix has something to deny.

    A settle posting is three legs. Six legs against one reference is what
    `nmigration`'s 01:38 query found 1,847 times.
    """
    item_id = seeded_batch.first_item_id
    processor = RecordingProcessorClient(latency_ms=400)
    sweep, retry = _build(sessions, processor)

    start = asyncio.Barrier(2)
    await asyncio.gather(
        _with_barrier(start, sweep.reconcile_batch(seeded_batch.id, trigger="manual")),
        _with_barrier(start, retry.retry_item(item_id, requested_by="test")),
    )

    assert await count_entries(reference_id=item_id) == 3


async def test_two_concurrent_retries_still_skip_each_other(
    sessions, seeded_batch, count_transactions
) -> None:
    """The property PAY-1607 actually delivered, and which was never in doubt.

    `SELECT ... FOR UPDATE SKIP LOCKED` does exclude a second `RetryScheduler`. The
    reviewer who wrote "nice, the SKIP LOCKED means the drain can't stomp on itself" was
    correct. He was answering a different question.
    """
    item_id = seeded_batch.first_item_id
    processor = RecordingProcessorClient(latency_ms=50, parties=1)
    _, retry_a = _build(sessions, processor)
    _, retry_b = _build(sessions, processor)

    start = asyncio.Barrier(2)
    first, second = await asyncio.gather(
        _with_barrier(start, retry_a.retry_item(item_id, requested_by="drain_a")),
        _with_barrier(start, retry_b.retry_item(item_id, requested_by="drain_b")),
    )

    claimed = [outcome for outcome in (first, second) if outcome is not None]
    assert len(claimed) == 1
    assert await count_transactions(purpose="settle", reference_id=item_id) == 1


async def test_sweep_alone_settles_every_item_in_the_batch(
    sessions, seeded_batch, count_transactions
) -> None:
    """Control case. With no drain running the sweep is correct on its own."""
    processor = RecordingProcessorClient(latency_ms=0, parties=1)
    sweep, _ = _build(sessions, processor)

    run = await sweep.reconcile_batch(seeded_batch.id, trigger="scheduled")

    assert run.items_settled == len(seeded_batch.retryable_item_ids)
    assert run.status == "succeeded"
    for item_id in seeded_batch.retryable_item_ids:
        assert await count_transactions(purpose="settle", reference_id=item_id) == 1
    assert len(processor.capture_calls) == len(seeded_batch.retryable_item_ids)


async def test_drain_alone_settles_every_item_in_the_batch(
    sessions, seeded_batch, count_transactions
) -> None:
    """The other control. A drain with no sweep is also correct on its own.

    Both halves are individually right. That is §5.1 of the-incident.md and it is why
    neither reviewer had anything to catch.
    """
    processor = RecordingProcessorClient(latency_ms=0, parties=1)
    _, retry = _build(sessions, processor)

    settled = await retry.drain(limit=50)

    assert settled == len(seeded_batch.retryable_item_ids)
    for item_id in seeded_batch.retryable_item_ids:
        assert await count_transactions(purpose="settle", reference_id=item_id) == 1
