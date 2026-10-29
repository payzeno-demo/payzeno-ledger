"""`POST /internal/v1/ops/jobs/{name}/run` — RFC platform-v2 §10.

Locally the stack runs on simulated time and sim-world advances the clock by days. Jobs on
intervals longer than a minute are then poked through this route, as
`X-Payzeno-Service: payzeno-ops`, rather than waited for. It runs one tick of the named job
in the request and answers with the job's `JobResult`.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from app.api.routers.ops import run_job
from app.api.schemas import JobRunResponse
from app.errors import NotFoundError
from app.middleware.internal_auth import ALLOWED_SERVICES
from app.workers.base import JobResult, PeriodicJob

pytestmark = pytest.mark.asyncio


class FakeImport(PeriodicJob):
    name: ClassVar[str] = "settlement_import"

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.runs = 0
        self.raises = raises

    @property
    def interval_seconds(self) -> int:
        return 3600

    async def run_once(self) -> JobResult:
        self.runs += 1
        if self.raises is not None:
            raise self.raises
        return JobResult(name=self.name, items_processed=4, duration_ms=12, error=None)


async def test_it_runs_one_tick_and_answers_with_the_job_result() -> None:
    job = FakeImport()

    body = await run_job({"settlement_import": job}, "payzeno-ops", "settlement_import")

    assert job.runs == 1
    JobRunResponse.model_validate(body)
    assert body["name"] == "settlement_import"
    assert body["items_processed"] == 4
    assert body["error"] is None


async def test_a_job_that_fails_answers_with_its_error_rather_than_a_500() -> None:
    job = FakeImport(raises=RuntimeError("worldflow exploded"))

    body = await run_job({"settlement_import": job}, "payzeno-ops", "settlement_import")

    assert body["items_processed"] == 0
    assert "worldflow exploded" in body["error"]


async def test_an_unknown_job_is_a_404_that_names_the_known_ones() -> None:
    with pytest.raises(NotFoundError) as raised:
        await run_job({"settlement_import": FakeImport()}, "payzeno-ops", "settlement_imports")

    assert raised.value.details["available"] == ["settlement_import"]


async def test_payzeno_ops_is_an_allowed_internal_caller() -> None:
    assert {"payzeno-api", "payzeno-billing-legacy", "payzeno-ops"} <= ALLOWED_SERVICES
