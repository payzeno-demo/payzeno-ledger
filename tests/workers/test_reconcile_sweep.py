"""`ReconciliationSweepJob` — app/workers/reconcile_sweep.py.

900 seconds, registered in every one of the four production tasks. Four unsynchronised
sweeps is fine; `ReconciliationService.reconcile_batch` takes a batch advisory lock and
they queue behind each other. What is not fine is a sweep running against the retry drain,
and that is not this file's problem — it is
`tests/integration/test_reconciliation_concurrency.py`'s.

What this file asserts is the job wrapper: which batches it picks up, that one bad batch
does not abort the pass, and that the interval comes off `Settings`.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.errors import BatchNotReconcilableError, ProcessorUnavailableError
from app.services.reconciliation.constants import RECONCILABLE_BATCH_STATUSES
from app.workers.reconcile_sweep import ReconciliationSweepJob
from tests.factories import make_batch

pytestmark = pytest.mark.asyncio


class StubBatches:
    def __init__(self, batches: list[Any]) -> None:
        self.batches = batches
        self.asked: list[tuple[str, ...]] = []

    async def list_by_status(self, session: Any, statuses: tuple[str, ...]) -> list[Any]:
        self.asked.append(statuses)
        return [batch for batch in self.batches if batch.status in statuses]


class StubReconciler:
    def __init__(self, *, fail_on: dict[str, Exception] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.fail_on = fail_on or {}

    async def reconcile_batch(
        self, batch_id: str, *, trigger: str = "scheduled", max_items: int = 5000
    ) -> Any:
        self.calls.append((batch_id, trigger, max_items))
        if batch_id in self.fail_on:
            raise self.fail_on[batch_id]
        return type(
            "ReconciliationRun",
            (),
            {
                "id": f"rr_{batch_id}",
                "batch_id": batch_id,
                "items_settled": 3,
                "items_failed": 0,
                "status": "succeeded",
            },
        )()


class Settings:
    reconcile_sweep_interval_seconds = 900
    reconcile_max_items_per_run = 500


def _job(batches: list[Any], reconciler: StubReconciler, sessions) -> ReconciliationSweepJob:
    return ReconciliationSweepJob(
        sessions=sessions,
        batches=StubBatches(batches),
        service=reconciler,
        settings=Settings(),
    )


def _batches() -> list[Any]:
    return [
        make_batch(batch_id="sb_open", status="open"),
        make_batch(batch_id="sb_closed", status="closed"),
        make_batch(batch_id="sb_partial", status="partially_reconciled"),
        make_batch(batch_id="sb_done", status="reconciled"),
    ]


async def test_interval_comes_from_settings(sessions_factory) -> None:
    job = _job(_batches(), StubReconciler(), sessions_factory)

    assert job.interval_seconds == 900
    assert job.name == "reconciliation_sweep"


async def test_sweep_only_picks_up_reconcilable_batches(sessions_factory) -> None:
    """`open` is still being imported into; `reconciled` is finished.

    Sweeping an open batch settles items the import is still writing, which is how you
    get half a batch settled against a file that has not finished parsing.
    """
    reconciler = StubReconciler()
    job = _job(_batches(), reconciler, sessions_factory)

    await job.run_once()

    assert [call[0] for call in reconciler.calls] == ["sb_closed", "sb_partial"]
    assert set(RECONCILABLE_BATCH_STATUSES) == {"closed", "partially_reconciled"}


async def test_sweep_passes_the_configured_item_ceiling(sessions_factory) -> None:
    reconciler = StubReconciler()
    job = _job(_batches(), reconciler, sessions_factory)

    await job.run_once()

    assert all(call[2] == 500 for call in reconciler.calls)
    assert all(call[1] == "scheduled" for call in reconciler.calls)


async def test_one_bad_batch_does_not_stop_the_pass(sessions_factory) -> None:
    """It gets picked up again in fifteen minutes, and the failure is on the run row.

    Aborting the pass would mean one wedged batch stops every other merchant from being
    settled, which is a much worse day than a wedged batch.
    """
    reconciler = StubReconciler(
        fail_on={"sb_closed": BatchNotReconcilableError("wedged", batch_id="sb_closed")}
    )
    job = _job(_batches(), reconciler, sessions_factory)

    result = await job.run_once()

    assert [call[0] for call in reconciler.calls] == ["sb_closed", "sb_partial"]
    assert result.error is None
    assert result.items_processed == 3


async def test_an_acquirer_outage_is_reported_not_raised(sessions_factory) -> None:
    reconciler = StubReconciler(
        fail_on={
            "sb_closed": ProcessorUnavailableError(code="processor_unavailable"),
            "sb_partial": ProcessorUnavailableError(code="processor_unavailable"),
        }
    )
    job = _job(_batches(), reconciler, sessions_factory)

    result = await job.run_once()

    assert result.items_processed == 0
    assert result.error is None


async def test_a_quiet_ledger_produces_an_empty_pass(sessions_factory) -> None:
    reconciler = StubReconciler()
    job = _job([make_batch(batch_id="sb_done", status="reconciled")], reconciler, sessions_factory)

    result = await job.run_once()

    assert reconciler.calls == []
    assert result.items_processed == 0
    assert result.name == "reconciliation_sweep"
