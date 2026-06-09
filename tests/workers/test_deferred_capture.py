"""`DeferredCaptureJob` — app/workers/deferred_capture.py.

PAY-2060. This job exists because PR #172 closed the concurrent-double-post window and
left the transactional-boundary window wide open: `capture_deferred` was still an external
HTTP call executed *inside* the open database transaction, after the claim. If that
transaction later aborted — deadlock, statement timeout, pool reset, task kill — the claim
rolled back and the cardholder had already been charged, with no ledger row and no record
that it happened.

So the capture moved out. `SettlementPoster` now writes a `capture_attempt` row with
status `pending` and a deterministic `acquirer_idempotency_key` and commits; this job, at
30 seconds, calls the acquirer with that key and records the outcome. Anything
indeterminate goes to `get_capture_status` rather than being re-issued, which is the
second double-charge mechanism neither #171 nor #172 touched.

Thirty seconds is a compromise. Cardholders notice a delayed capture; merchants notice a
duplicated one much more.
"""

from __future__ import annotations

import pytest

from app.errors import ProcessorIndeterminateError, ProcessorUnavailableError
from app.services.reconciliation.constants import RETRYABLE_ERROR_CODES
from app.workers.deferred_capture import BATCH_SIZE, INTERVAL_SECONDS, DeferredCaptureJob

pytestmark = pytest.mark.asyncio


class StubCaptures:
    """`DeferredCaptureService.process_pending` — issue pending, resolve indeterminate."""

    def __init__(self, *, processed: int = 0, raises: Exception | None = None) -> None:
        self.processed = processed
        self.raises = raises
        self.calls: list[int] = []

    async def process_pending(self, *, limit: int) -> int:
        self.calls.append(limit)
        if self.raises is not None:
            raise self.raises
        return self.processed


class Settings:
    deferred_capture_interval_seconds = 30
    deferred_capture_batch_size = 100


async def test_interval_is_thirty_seconds() -> None:
    job = DeferredCaptureJob(captures=StubCaptures(), settings=Settings())

    assert job.interval_seconds == INTERVAL_SECONDS == 30
    assert job.name == "deferred_capture"


async def test_it_processes_pending_attempts() -> None:
    service = StubCaptures(processed=11)
    job = DeferredCaptureJob(captures=service, settings=Settings())

    result = await job.run_once()

    assert result.items_processed == 11
    assert service.calls == [BATCH_SIZE]


async def test_a_processor_outage_does_not_kill_the_job() -> None:
    """During a degradation this job is the one that must keep its shape.

    It is holding the only record that a capture was intended, so a crash loop here is
    the difference between a delayed capture and a lost one.
    """
    service = StubCaptures(raises=ProcessorUnavailableError(code="processor_unavailable"))
    job = DeferredCaptureJob(captures=service, settings=Settings())

    result = await job._tick()  # noqa: SLF001

    assert result.error == "processor_unavailable"
    assert result.items_processed == 0
    assert result.name == "deferred_capture"


async def test_processor_timeout_is_still_classed_as_retryable() -> None:
    """This is the half of PAY-2060 that has not landed, pinned so it is not forgotten.

    The job and the `capture_attempt` table shipped; the classification did not.
    `processor_timeout` is still in `RETRYABLE_ERROR_CODES`, which means a capture that
    timed out — the one state where nobody knows whether the cardholder was charged —
    still goes down the retry path from the settlement side. `DeferredCaptureService`
    resolves it correctly through `get_capture_status` once an attempt row exists, so the
    window is narrower than it was, but it is not closed.

    When the constants move, this test fails, and the person moving them reads the
    docstring instead of guessing. That is the whole point of pinning it.
    """
    assert "processor_timeout" in RETRYABLE_ERROR_CODES
    assert not hasattr(
        __import__(
            "app.services.reconciliation.constants", fromlist=["constants"]
        ),
        "INDETERMINATE_ERROR_CODES",
    )


async def test_an_indeterminate_error_never_crashes_the_scheduler() -> None:
    """`_tick` swallows every `PayzenoLedgerError` and reports it on the result.

    A job that raises out of its tick takes APScheduler's worker with it, and the next
    thing anybody notices is that captures stopped happening two hours ago.
    """
    service = StubCaptures(raises=ProcessorIndeterminateError("capture outcome unknown"))
    job = DeferredCaptureJob(captures=service, settings=Settings())

    result = await job._tick()  # noqa: SLF001

    assert result.error is not None
    assert result.items_processed == 0


async def test_a_quiet_pass_costs_one_query() -> None:
    """It runs 2,880 times a day. `pix_capture_attempt_pending` is why that is fine."""
    service = StubCaptures(processed=0)
    job = DeferredCaptureJob(captures=service, settings=Settings())

    result = await job.run_once()

    assert result.items_processed == 0
    assert len(service.calls) == 1
