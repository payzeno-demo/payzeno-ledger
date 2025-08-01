"""Service-layer fixtures.

The services under test are the real classes. What is replaced is SQL: every repository
here SUBCLASSES its production repository and overrides the query methods with dictionary
lookups, so the service still calls `self._items.get_or_raise(session, item_id)` and still
gets the production error type back when the row is missing.

`SharedSessionFactory` hands out one session object to every caller. That is a faithful
model of what the suite did before PAY-2053 and it is the reason the sequential idempotency
tests in test_retry.py and test_reconciler.py pass on the buggy code: with one session there
is one transaction, and the second reader sees the first writer's row. Real concurrency
lives in tests/integration/test_reconciliation_concurrency.py against real Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import pytest

from app.errors import (
    BatchNotFoundError,
    ChargeProjectionNotFoundError,
    NotFoundError,
    ReconciliationItemNotFoundError,
)
from app.models.reconciliation_run import ReconciliationRun
from app.ports import SessionFactory
from app.repositories.ledger_transaction import LedgerTransactionRepository
from app.repositories.projections import MerchantProjectionRepository
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.reconciliation_run import ReconciliationRunRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.repositories.settlement_charge import SettlementChargeRepository
from app.services.transactions import LedgerPoster, PostResult
from tests.factories import make_batch, make_charge_projection, make_item, make_merchant_projection

FIXED_NOW = datetime(2026, 4, 16, 0, 15, tzinfo=UTC)


class FakeSession:
    """Stands in for `AsyncSession`.

    It records the SQL-shaped calls a service makes directly (there are very few — services
    go through repositories) and otherwise does nothing. `rollback` flips a flag the
    out-of-band failure-branch tests assert on.
    """

    def __init__(self, name: str = "session") -> None:
        self.name = name
        self.executed: list[str] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append(str(statement))
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class SharedSessionFactory(SessionFactory):
    """One session for everybody — the pre-PAY-2053 fixture, deliberately."""

    def __init__(self) -> None:
        self.session = FakeSession()
        self.begin_count = 0

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[FakeSession]:
        self.begin_count += 1
        try:
            yield self.session
        except Exception:
            await self.session.rollback()
            raise
        else:
            await self.session.commit()


class InMemoryItemRepository(ReconciliationItemRepository):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, Any] = {}

    def seed(self, item: Any) -> Any:
        self.rows[item.id] = item
        return item

    async def get(self, session: Any, entity_id: str) -> Any | None:
        return self.rows.get(entity_id)

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        try:
            return self.rows[entity_id]
        except KeyError:
            raise ReconciliationItemNotFoundError(entity_id=entity_id) from None

    async def list_for_settlement(
        self, session: Any, *, batch_id: str, statuses: frozenset[str], limit: int
    ) -> list[Any]:
        found = [
            row
            for row in self.rows.values()
            if row.batch_id == batch_id and row.status in statuses
        ]
        return sorted(found, key=lambda r: r.id)[:limit]

    async def list_retryable_ids(self, session: Any, *, limit: int, now: datetime | None = None) -> list[str]:
        due = now or FIXED_NOW
        found = [
            row.id
            for row in self.rows.values()
            if row.status in {"pending", "retryable"} and row.next_attempt_at <= due
        ]
        return sorted(found)[:limit]

    async def get_batch_id(self, session: Any, item_id: str) -> str | None:
        row = self.rows.get(item_id)
        return row.batch_id if row is not None else None


class InMemoryBatchRepository(SettlementBatchRepository):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, Any] = {}

    def seed(self, batch: Any) -> Any:
        self.rows[batch.id] = batch
        return batch

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        try:
            return self.rows[entity_id]
        except KeyError:
            raise BatchNotFoundError(entity_id=entity_id) from None

    async def list_by_status(self, session: Any, statuses: tuple[str, ...]) -> list[Any]:
        return [row for row in self.rows.values() if row.status in statuses]

    async def add_posted_total(self, session: Any, batch_id: str, *, amount_minor: int) -> None:
        self.rows[batch_id].posted_total_minor += amount_minor

    async def mark_status(self, session: Any, batch_id: str, *, status: str) -> Any:
        row = self.rows[batch_id]
        row.status = status
        return row


class InMemoryRunRepository(ReconciliationRunRepository):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, ReconciliationRun] = {}
        self._seq = 0

    async def start(self, session: Any, *, batch_id: str, trigger: str) -> ReconciliationRun:
        self._seq += 1
        run = ReconciliationRun(
            id=f"rr_{self._seq:04d}",
            batch_id=batch_id,
            trigger=trigger,
            status="running",
            items_total=0,
            items_settled=0,
            items_failed=0,
            started_at=FIXED_NOW,
        )
        self.rows[run.id] = run
        return run

    async def finish(
        self,
        session: Any,
        run_id: str,
        *,
        items_total: int,
        items_settled: int,
        items_failed: int,
        status: str,
        error_summary: str | None = None,
    ) -> ReconciliationRun:
        run = self.rows[run_id]
        run.items_total = items_total
        run.items_settled = items_settled
        run.items_failed = items_failed
        run.status = status
        run.error_summary = error_summary
        run.finished_at = FIXED_NOW
        return run

    async def find_active(self, session: Any, *, batch_id: str) -> ReconciliationRun | None:
        for run in self.rows.values():
            if run.batch_id == batch_id and run.status == "running":
                return run
        return None


class InMemoryTransactionRepository(LedgerTransactionRepository):
    def __init__(self) -> None:
        super().__init__()
        self.by_key: dict[str, Any] = {}

    async def find_by_idempotency_key(self, session: Any, key: str) -> Any | None:
        return self.by_key.get(key)


class InMemoryChargeRepository(SettlementChargeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, Any] = {}

    def seed(self, charge: Any) -> Any:
        self.rows[charge.charge_id] = charge
        return charge

    async def get_or_raise(self, session: Any, charge_id: str) -> Any:
        try:
            return self.rows[charge_id]
        except KeyError:
            raise ChargeProjectionNotFoundError(entity_id=charge_id) from None


class InMemoryMerchantRepository(MerchantProjectionRepository):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, Any] = {}

    def seed(self, merchant: Any) -> Any:
        self.rows[merchant.merchant_id] = merchant
        return merchant

    async def get_or_raise(self, session: Any, merchant_id: str) -> Any:
        try:
            return self.rows[merchant_id]
        except KeyError:
            raise NotFoundError(entity_id=merchant_id) from None


class RecordingLedgerPoster(LedgerPoster):
    """The real class's interface, an in-memory idempotency map behind it."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.by_key: dict[str, Any] = {}
        self._seq = 0

    async def post(self, session: Any, **kwargs: Any) -> PostResult:
        key = kwargs["idempotency_key"]
        existing = self.by_key.get(key)
        if existing is not None:
            if kwargs.get("on_conflict", "raise") == "return_existing":
                return PostResult(transaction=existing, created=False)
            from app.errors import DuplicateSettlementError

            raise DuplicateSettlementError(existing_transaction_id=existing.id)

        self._seq += 1
        transaction = type("Txn", (), {"id": f"txn_{self._seq:04d}", **kwargs})()
        self.by_key[key] = transaction
        self.posted.append(kwargs)
        return PostResult(transaction=transaction, created=True)


@pytest.fixture
def sessions_factory() -> SharedSessionFactory:
    return SharedSessionFactory()


@pytest.fixture
def items() -> InMemoryItemRepository:
    return InMemoryItemRepository()


@pytest.fixture
def batches() -> InMemoryBatchRepository:
    return InMemoryBatchRepository()


@pytest.fixture
def runs() -> InMemoryRunRepository:
    return InMemoryRunRepository()


@pytest.fixture
def transactions() -> InMemoryTransactionRepository:
    return InMemoryTransactionRepository()


@pytest.fixture
def charges() -> InMemoryChargeRepository:
    repo = InMemoryChargeRepository()
    repo.seed(make_charge_projection(charge_id="ch_default", capture_at_settlement=False))
    return repo


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    repo = InMemoryMerchantRepository()
    repo.seed(make_merchant_projection(merchant_id="mer_default", settlement_tolerance_minor=100))
    return repo


@pytest.fixture
def ledger() -> RecordingLedgerPoster:
    return RecordingLedgerPoster()


@pytest.fixture
def seeded_item(items: InMemoryItemRepository, batches: InMemoryBatchRepository) -> Any:
    batches.seed(make_batch(batch_id="sb_svc", status="closed"))
    return items.seed(
        make_item(
            item_id="ri_svc",
            batch_id="sb_svc",
            charge_id="ch_default",
            merchant_id="mer_default",
            status="retryable",
            next_attempt_at=FIXED_NOW,
        )
    )
