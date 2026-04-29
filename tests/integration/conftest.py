"""Integration fixtures — the ones that need a real Postgres.

PAY-2053. Everything above this directory runs against `tests/services/conftest.py`'s
`SharedSessionFactory`, which hands one session object to every caller. That fixture is a
faithful model of what the suite had for the first nine months, and it is exactly why
`test_retry_is_idempotent` passed on the buggy code: one session means one transaction,
and the second reader sees the first writer's uncommitted row.

Concurrency cannot be expressed there. Two connections is the entire premise of PAY-2041,
so this layer builds a real `PooledSessionFactory` over a real engine and seeds real rows.

`pg_engine` itself is session-scoped and lives in `tests/conftest.py` — it is shared with
the repository layer, which also needs real SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from app.db.session import PooledSessionFactory
from app.repositories.projections import MerchantProjectionRepository
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.repositories.settlement_charge import SettlementChargeRepository
from tests.factories import (
    make_batch,
    make_charge_projection,
    make_item,
    make_merchant_projection,
)

#: The seeded rows are dated to the night of PAY-2041 on purpose. When someone opens a
#: failing integration test at 3am and sees 00:15Z, the runbook is one grep away.
INCIDENT_NIGHT = datetime(2026, 1, 22, 0, 15, tzinfo=UTC)


@dataclass(slots=True)
class SeededBatch:
    """What `seeded_batch` hands back.

    `retryable_item_ids` is ordered and stable so a test can address a specific item
    without re-querying — `seeded_batch.retryable_item_ids[0]` is the item both the sweep
    and the retry will reach for.
    """

    id: str
    merchant_id: str
    currency: str
    retryable_item_ids: list[str] = field(default_factory=list)
    charge_ids: list[str] = field(default_factory=list)

    @property
    def first_item_id(self) -> str:
        return self.retryable_item_ids[0]


@pytest.fixture
async def sessions(pg_engine) -> PooledSessionFactory:
    """A real session factory: every `begin()` checks out its own pooled connection.

    This is the fixture that makes the race writable. `interfaces.md` §3.4 is explicit
    that sessions are never nested onto one connection — a reentrant factory would either
    deadlock or silently share a transaction, and then the defect evaporates.
    """
    return PooledSessionFactory(pg_engine)


@pytest.fixture
async def seeded_batch(sessions: PooledSessionFactory) -> SeededBatch:
    """A closed batch with four retryable items, all deferred-capture merchants.

    Deferred capture matters: it is what turns a duplicate ledger row into a duplicate on
    somebody's card statement, and the regression test asserts on `capture_calls`.
    """
    batch_id = "sb_INTEG_QK"
    merchant_id = "mer_integ_loomcraft"
    currency = "USD"
    seeded = SeededBatch(id=batch_id, merchant_id=merchant_id, currency=currency)

    batches = SettlementBatchRepository()
    items = ReconciliationItemRepository()
    charges = SettlementChargeRepository()
    merchants = MerchantProjectionRepository()

    async with sessions.begin() as session:
        await merchants.upsert_if_newer(
            session,
            make_merchant_projection(
                merchant_id=merchant_id,
                status="active",
                settlement_tolerance_minor=100,
                capture_at_settlement=True,
                source_occurred_at=INCIDENT_NIGHT,
            ),
        )
        await batches.add(
            session,
            make_batch(
                batch_id=batch_id,
                acquirer="worldflow",
                currency=currency,
                status="closed",
                processing_date=date(2026, 1, 21),
                file_reference="WF-20260121-QK",
            ),
        )

        for index in range(4):
            charge_id = f"ch_integ_{index}"
            item_id = f"ri_integ_{index}"
            await charges.add(
                session,
                make_charge_projection(
                    charge_id=charge_id,
                    merchant_id=merchant_id,
                    currency=currency,
                    amount_minor=10_000,
                    capture_at_settlement=True,
                    processor_reference=f"WF-8831-{index}",
                    source_occurred_at=INCIDENT_NIGHT,
                ),
            )
            await items.add(
                session,
                make_item(
                    item_id=item_id,
                    batch_id=batch_id,
                    charge_id=charge_id,
                    merchant_id=merchant_id,
                    currency=currency,
                    status="retryable",
                    line_type="sale",
                    gross_minor=10_000,
                    fee_minor=290,
                    net_minor=9_710,
                    variance_minor=0,
                    acquirer_reference=f"WF-8831-{index}",
                    next_attempt_at=INCIDENT_NIGHT,
                ),
            )
            seeded.charge_ids.append(charge_id)
            seeded.retryable_item_ids.append(item_id)

    return seeded


@pytest.fixture
async def count_transactions(pg_engine):
    """Count `ledger_transaction` rows straight from SQL.

    Deliberately not through `LedgerTransactionRepository`: the thing under test is
    whether two rows exist, and `find_by_idempotency_key` uses `.scalars().first()` and
    would happily report one when there are two. That is §3.6 of the-incident.md and it
    is why the assertion has to bypass the repository.
    """

    async def _count(*, purpose: str, reference_id: str) -> int:
        async with pg_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT count(*) FROM ledger_transaction "
                    "WHERE purpose = :purpose AND reference_id = :reference_id"
                ),
                {"purpose": purpose, "reference_id": reference_id},
            )
            return int(result.scalar_one())

    return _count


@pytest.fixture
async def count_entries(pg_engine):
    """Three entries per settle transaction. Six means it happened twice."""

    async def _count(*, reference_id: str) -> int:
        async with pg_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT count(*) FROM ledger_entry e "
                    "JOIN ledger_transaction t ON t.id = e.transaction_id "
                    "WHERE t.reference_id = :reference_id"
                ),
                {"reference_id": reference_id},
            )
            return int(result.scalar_one())

    return _count
