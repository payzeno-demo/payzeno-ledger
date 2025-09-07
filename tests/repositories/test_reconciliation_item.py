"""`ReconciliationItemRepository` — app/repositories/reconciliation_item.py.

Three methods matter and all three have history attached:

* `list_for_settlement(batch_id, statuses, limit)` — the sweep's read. Plain SELECT, no row
  locks, because the sweep serialises on the batch advisory lock instead.
* `list_retryable_ids(limit)` — the drain's read. Scans `pix_reconciliation_item_retryable`
  and filters `next_attempt_at <= now()`. Before migration 0014 this seq-scanned 2.4M rows
  at a p99 of 12 seconds, which is the only reason the drain never used to keep up with the
  sweep. Making it fast is what made the two paths genuinely concurrent.
* `get_batch_id(item_id)` — added by PAY-2043 at 02:00 so `_claim_item` can ask for the
  batch advisory lock before it takes the row lock. Four lines, and it is the fix.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.errors import ReconciliationItemNotFoundError
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.services.reconciliation.constants import RETRYABLE_STATUSES
from tests.factories import make_batch, make_item

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 4, 16, 0, 15, tzinfo=UTC)


@pytest.fixture
def repo() -> ReconciliationItemRepository:
    return ReconciliationItemRepository()


async def _seed(session, batch_id: str = "sb_ri_1") -> str:
    session.add(make_batch(batch_id=batch_id, status="closed"))
    await session.flush()
    return batch_id


async def test_add_and_get(session, repo: ReconciliationItemRepository) -> None:
    batch_id = await _seed(session, "sb_ri_add")
    await repo.add(session, make_item(item_id="ri_1", batch_id=batch_id))
    await session.flush()

    assert (await repo.get_or_raise(session, "ri_1")).batch_id == batch_id


async def test_get_or_raise_on_a_missing_item(session, repo: ReconciliationItemRepository) -> None:
    with pytest.raises(ReconciliationItemNotFoundError):
        await repo.get_or_raise(session, "ri_nope")


async def test_acquirer_reference_is_unique_within_a_batch(
    session, repo: ReconciliationItemRepository
) -> None:
    batch_id = await _seed(session, "sb_ri_uq")
    await repo.add(session, make_item(item_id="ri_2", batch_id=batch_id, acquirer_reference="WF-1"))
    await session.flush()

    await repo.add(session, make_item(item_id="ri_3", batch_id=batch_id, acquirer_reference="WF-1"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_the_same_charge_may_appear_in_two_batches(
    session, repo: ReconciliationItemRepository
) -> None:
    """There is deliberately NO unique index on charge_id, and there cannot be.

    A chargeback representment legitimately re-settles the same charge in a later batch. So
    "the table would have stopped the duplicate" was never true, and a reviewer looking only
    at this table reasonably concludes the design is fine.
    """
    first = await _seed(session, "sb_ri_orig")
    second = await _seed(session, "sb_ri_repr")

    await repo.add(session, make_item(item_id="ri_4", batch_id=first, charge_id="ch_SAME"))
    await repo.add(session, make_item(item_id="ri_5", batch_id=second, charge_id="ch_SAME"))
    await session.flush()  # no constraint fires, and that is correct


async def test_list_for_settlement_filters_on_status_and_batch(
    session, repo: ReconciliationItemRepository
) -> None:
    batch_id = await _seed(session, "sb_ri_list")
    other = await _seed(session, "sb_ri_other")

    await repo.add(session, make_item(item_id="ri_p", batch_id=batch_id, status="pending"))
    await repo.add(session, make_item(item_id="ri_r", batch_id=batch_id, status="retryable"))
    await repo.add(session, make_item(item_id="ri_s", batch_id=batch_id, status="settled"))
    await repo.add(session, make_item(item_id="ri_x", batch_id=other, status="pending"))
    await session.flush()

    items = await repo.list_for_settlement(
        session, batch_id=batch_id, statuses=RETRYABLE_STATUSES, limit=100
    )

    assert {i.id for i in items} == {"ri_p", "ri_r"}


async def test_list_for_settlement_respects_the_limit(
    session, repo: ReconciliationItemRepository
) -> None:
    batch_id = await _seed(session, "sb_ri_limit")
    for n in range(10):
        await repo.add(session, make_item(item_id=f"ri_lim_{n}", batch_id=batch_id))
    await session.flush()

    items = await repo.list_for_settlement(
        session, batch_id=batch_id, statuses=RETRYABLE_STATUSES, limit=4
    )
    assert len(items) == 4


async def test_list_for_settlement_takes_no_row_locks(
    sessions, repo: ReconciliationItemRepository
) -> None:
    """The sweep's read is a plain SELECT — step t1 of the interleaving.

    Two sessions can both read the same item and neither blocks. That is by design: the
    sweep excludes other sweeps with the batch advisory lock, not with row locks. It is also
    exactly why a drain holding a FOR UPDATE lock on the same row skipped nothing.
    """
    async with sessions.begin() as setup:
        batch_id = await _seed(setup, "sb_ri_nolock")
        await repo.add(setup, make_item(item_id="ri_nolock", batch_id=batch_id))

    async with sessions.begin() as first:
        first_read = await repo.list_for_settlement(
            first, batch_id=batch_id, statuses=RETRYABLE_STATUSES, limit=10
        )
        async with sessions.begin() as second:
            second_read = await repo.list_for_settlement(
                second, batch_id=batch_id, statuses=RETRYABLE_STATUSES, limit=10
            )

    assert [i.id for i in first_read] == [i.id for i in second_read] == ["ri_nolock"]


async def test_list_retryable_ids_skips_items_not_yet_due(
    session, repo: ReconciliationItemRepository
) -> None:
    batch_id = await _seed(session, "sb_ri_due")
    await repo.add(
        session,
        make_item(item_id="ri_due", batch_id=batch_id, status="retryable", next_attempt_at=NOW),
    )
    await repo.add(
        session,
        make_item(
            item_id="ri_later",
            batch_id=batch_id,
            status="retryable",
            next_attempt_at=NOW + timedelta(hours=1),
        ),
    )
    await session.flush()

    ids = await repo.list_retryable_ids(session, limit=50, now=NOW)

    assert ids == ["ri_due"]


async def test_list_retryable_ids_returns_ids_not_hydrated_rows(
    session, repo: ReconciliationItemRepository
) -> None:
    # The drain re-reads each item inside its own transaction with FOR UPDATE. Handing it
    # hydrated objects from a different session would make the claim read stale data.
    batch_id = await _seed(session, "sb_ri_ids")
    await repo.add(session, make_item(item_id="ri_ids", batch_id=batch_id, status="retryable"))
    await session.flush()

    ids = await repo.list_retryable_ids(session, limit=50, now=NOW)
    assert ids == ["ri_ids"]
    assert all(isinstance(i, str) for i in ids)


async def test_get_batch_id_returns_the_parent(session, repo: ReconciliationItemRepository) -> None:
    batch_id = await _seed(session, "sb_ri_parent")
    await repo.add(session, make_item(item_id="ri_parent", batch_id=batch_id))
    await session.flush()

    assert await repo.get_batch_id(session, "ri_parent") == batch_id


async def test_get_batch_id_returns_none_for_a_missing_item(
    session, repo: ReconciliationItemRepository
) -> None:
    # `_claim_item` treats None as "not claimable" and returns None to the caller, which the
    # route maps onto 409 settlement_locked. It must not raise.
    assert await repo.get_batch_id(session, "ri_gone") is None


async def test_mark_settled_records_the_transaction(
    session, repo: ReconciliationItemRepository
) -> None:
    batch_id = await _seed(session, "sb_ri_settle")
    await repo.add(session, make_item(item_id="ri_settle", batch_id=batch_id))
    await session.flush()

    item = await repo.mark_settled(session, "ri_settle", transaction_id="txn_1", at=NOW)

    assert item.status == "settled"
    assert item.settled_transaction_id == "txn_1"
    assert item.last_attempt_at == NOW


async def test_count_by_status_backs_the_backlog_endpoint(
    session, repo: ReconciliationItemRepository
) -> None:
    batch_id = await _seed(session, "sb_ri_count")
    await repo.add(session, make_item(item_id="ri_c1", batch_id=batch_id, status="retryable"))
    await repo.add(session, make_item(item_id="ri_c2", batch_id=batch_id, status="retryable"))
    await repo.add(session, make_item(item_id="ri_c3", batch_id=batch_id, status="orphaned"))
    await session.flush()

    counts = await repo.count_by_status(session, batch_id=batch_id)

    assert counts["retryable"] == 2
    assert counts["orphaned"] == 1
