"""`ReconciliationRunRepository` — app/repositories/reconciliation_run.py.

`start` and `finish` are separate methods on purpose. `reconcile_batch` opens the run in one
short transaction, does the whole pass across many other sessions, and then closes the run in
a third. Carrying the ORM object across those boundaries and mutating it would be mutating a
detached instance — which is why `finish` takes the counters explicitly instead of reading
them back off `run`.

`items_total` is NOT NULL and has a downstream consumer in `settlement.completed`. A naive
implementation never sets it and the column defaults to 0 forever.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.repositories.reconciliation_run import ReconciliationRunRepository
from tests.factories import make_batch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 4, 16, 0, 15, tzinfo=UTC)


@pytest.fixture
def repo() -> ReconciliationRunRepository:
    return ReconciliationRunRepository()


async def _seed_batch(session, batch_id: str) -> str:
    session.add(make_batch(batch_id=batch_id, status="closed"))
    await session.flush()
    return batch_id


async def test_start_creates_a_running_run(session, repo: ReconciliationRunRepository) -> None:
    batch_id = await _seed_batch(session, "sb_rr_1")

    run = await repo.start(session, batch_id=batch_id, trigger="scheduled")

    assert run.status == "running"
    assert run.batch_id == batch_id
    assert run.trigger == "scheduled"
    assert run.started_at is not None
    assert run.finished_at is None


async def test_finish_records_the_counters_passed_in(
    session, repo: ReconciliationRunRepository
) -> None:
    batch_id = await _seed_batch(session, "sb_rr_2")
    run = await repo.start(session, batch_id=batch_id, trigger="manual")
    await session.flush()

    finished = await repo.finish(
        session,
        run.id,
        items_total=4_113,
        items_settled=4_100,
        items_failed=13,
        status="failed",
        error_summary="processor_unavailable x13",
    )

    assert finished.items_total == 4_113
    assert finished.items_settled == 4_100
    assert finished.items_failed == 13
    assert finished.status == "failed"
    assert finished.error_summary == "processor_unavailable x13"
    assert finished.finished_at is not None


async def test_items_total_is_set_even_on_an_empty_pass(
    session, repo: ReconciliationRunRepository
) -> None:
    batch_id = await _seed_batch(session, "sb_rr_3")
    run = await repo.start(session, batch_id=batch_id, trigger="scheduled")
    await session.flush()

    finished = await repo.finish(
        session, run.id, items_total=0, items_settled=0, items_failed=0, status="succeeded"
    )

    assert finished.items_total == 0
    assert finished.status == "succeeded"


async def test_only_one_run_per_batch_may_be_running(
    session, repo: ReconciliationRunRepository
) -> None:
    """`pix_reconciliation_run_active (batch_id) where status = 'running'` is unique.

    This is also the row PAY-2057 wants the drain to consult: instead of discovering a
    running sweep lock by lock, 200 attempts at a time, just look for an active run on the
    batch and skip. That ticket is still open.
    """
    batch_id = await _seed_batch(session, "sb_rr_4")
    await repo.start(session, batch_id=batch_id, trigger="scheduled")
    await session.flush()

    from sqlalchemy.exc import IntegrityError

    await repo.start(session, batch_id=batch_id, trigger="manual")
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_finished_run_frees_the_partial_index_slot(
    session, repo: ReconciliationRunRepository
) -> None:
    batch_id = await _seed_batch(session, "sb_rr_5")
    first = await repo.start(session, batch_id=batch_id, trigger="scheduled")
    await session.flush()
    await repo.finish(
        session, first.id, items_total=1, items_settled=1, items_failed=0, status="succeeded"
    )
    await session.flush()

    second = await repo.start(session, batch_id=batch_id, trigger="retry")
    await session.flush()

    assert second.id != first.id


async def test_find_active_for_batch(session, repo: ReconciliationRunRepository) -> None:
    batch_id = await _seed_batch(session, "sb_rr_6")
    started = await repo.start(session, batch_id=batch_id, trigger="scheduled")
    await session.flush()

    active = await repo.find_active(session, batch_id=batch_id)
    assert active is not None
    assert active.id == started.id

    await repo.finish(
        session, started.id, items_total=0, items_settled=0, items_failed=0, status="succeeded"
    )
    await session.flush()

    assert await repo.find_active(session, batch_id=batch_id) is None


async def test_list_recent_orders_newest_first(session, repo: ReconciliationRunRepository) -> None:
    batch_id = await _seed_batch(session, "sb_rr_7")
    ids: list[str] = []
    for _ in range(3):
        run = await repo.start(session, batch_id=batch_id, trigger="scheduled")
        await session.flush()
        await repo.finish(
            session, run.id, items_total=0, items_settled=0, items_failed=0, status="succeeded"
        )
        await session.flush()
        ids.append(run.id)

    recent = await repo.list_recent(session, batch_id=batch_id, limit=10)

    assert [r.id for r in recent][:3] == list(reversed(ids))


async def test_the_active_index_actually_exists(session) -> None:
    rows = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = 'reconciliation_run'")
    )
    assert "pix_reconciliation_run_active" in {row[0] for row in rows}
