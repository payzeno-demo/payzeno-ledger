"""`SettlementBatchRepository` — app/repositories/settlement_batch.py.

`list_by_status` is what `ReconciliationSweepJob` calls every 900 seconds to decide what to
work on, and it is the reason `partially_reconciled` has to be in that tuple: a batch with
retryable items left over is exactly the batch the sweep needs to come back to. It is also,
therefore, the state the arc INC race needs, and it is entirely normal.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.errors import BatchNotFoundError
from app.repositories.settlement_batch import SettlementBatchRepository
from tests.factories import make_batch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def repo() -> SettlementBatchRepository:
    return SettlementBatchRepository()


async def test_file_reference_is_unique_per_acquirer(
    session, repo: SettlementBatchRepository
) -> None:
    """`uq_settlement_batch_file (acquirer, file_reference)`.

    The import job is idempotent because of this index and not because of anything clever in
    the service: re-running an import for the same acquirer file cannot open a second batch.
    Two acquirers may legitimately use the same file name, hence the composite.
    """
    await repo.add(
        session, make_batch(batch_id="sb_rb_2", acquirer="worldflow", file_reference="WF-20260415")
    )
    await session.flush()

    await repo.add(
        session, make_batch(batch_id="sb_rb_3", acquirer="nordpay", file_reference="WF-20260415")
    )
    await session.flush()  # different acquirer, fine

    await repo.add(
        session, make_batch(batch_id="sb_rb_4", acquirer="worldflow", file_reference="WF-20260415")
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_list_by_status_is_what_the_sweep_calls(
    session, repo: SettlementBatchRepository
) -> None:
    await repo.add(session, make_batch(batch_id="sb_rb_open", status="open"))
    await repo.add(session, make_batch(batch_id="sb_rb_closed", status="closed"))
    await repo.add(session, make_batch(batch_id="sb_rb_partial", status="partially_reconciled"))
    await repo.add(session, make_batch(batch_id="sb_rb_done", status="reconciled"))
    await session.flush()

    found = await repo.list_by_status(session, ("closed", "partially_reconciled"))
    ids = {b.id for b in found}

    assert ids == {"sb_rb_closed", "sb_rb_partial"}
    assert "sb_rb_open" not in ids
    assert "sb_rb_done" not in ids


async def test_list_by_status_with_an_empty_tuple_returns_nothing(
    session, repo: SettlementBatchRepository
) -> None:
    await repo.add(session, make_batch(batch_id="sb_rb_5", status="closed"))
    await session.flush()

    assert await repo.list_by_status(session, ()) == []


async def test_add_posted_total_accumulates(session, repo: SettlementBatchRepository) -> None:
    # `posted_total_minor` vs `expected_total_minor` is what PAY-2055's second alarm reads
    # (posted/expected > 1.001). On the incident night it was inflated by net_minor for each
    # of the 1,847 duplicates and nothing was watching it.
    batch = make_batch(batch_id="sb_rb_6", expected_total_minor=100_000)
    await repo.add(session, batch)
    await session.flush()

    await repo.add_posted_total(session, "sb_rb_6", amount_minor=40_000)
    await repo.add_posted_total(session, "sb_rb_6", amount_minor=60_000)

    refreshed = await repo.get_or_raise(session, "sb_rb_6")
    assert refreshed.posted_total_minor == 100_000
    assert refreshed.posted_total_minor <= refreshed.expected_total_minor


async def test_mark_status_transitions(session, repo: SettlementBatchRepository) -> None:
    batch = make_batch(batch_id="sb_rb_7", status="open")
    await repo.add(session, batch)
    await session.flush()

    closed = await repo.mark_status(session, "sb_rb_7", status="closed")
    assert closed.status == "closed"
    assert closed.closed_at is not None

    reconciled = await repo.mark_status(session, "sb_rb_7", status="reconciled")
    assert reconciled.reconciled_at is not None


async def test_list_unfunded_uses_the_partial_index_predicate(
    session, repo: SettlementBatchRepository
) -> None:
    await repo.add(
        session,
        make_batch(batch_id="sb_rb_8", status="reconciled", processing_date=date(2026, 4, 15)),
    )
    await repo.add(session, make_batch(batch_id="sb_rb_9", status="open"))
    await session.flush()

    unfunded = await repo.list_unfunded(session)

    assert [b.id for b in unfunded] == ["sb_rb_8"]
