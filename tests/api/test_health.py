"""`app/api/routers/health.py` — api-surface.md §10.5.

Three public paths on a service that is otherwise entirely behind
`InternalAuthMiddleware`: `/healthz`, `/readyz`, and the `/metrics` mount in `app/main.py`.
They are named in `app.middleware.internal_auth.PUBLIC_PATHS`, and the first test here
asserts exactly that, because "the health check started 403ing after a middleware change"
is a real outage shape: ECS reads a 403 as unhealthy and replaces every task in the
service, one rolling batch at a time, while the process itself is fine.

The second thing this file exists to pin down is the split between the two probes.
`/healthz` answers from memory. `/readyz` touches the database. Wiring the database check
into liveness is the classic version of this mistake — during a Postgres failover every
task fails liveness at the same moment, ECS kills all four, and the outage outlives the
failover by however long a cold start takes. `test_liveness_never_opens_a_session` is a
signature assertion rather than a behavioural one on purpose: the only way to make
`/healthz` touch the database is to give it a `SessionFactory`, so the absence of that
parameter is the property worth locking.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import Response, status

from app.api.routers import health
from app.api.routers.health import (
    READINESS_BUDGET_MS,
    SERVICE_NAME,
    healthz,
    readyz,
)
from app.middleware.internal_auth import PUBLIC_PATHS

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------------------
# the exemption
# --------------------------------------------------------------------------------------


def test_both_probes_are_exempt_from_internal_auth() -> None:
    """The ECS health check cannot present mTLS plus a service header plus a secret."""
    assert "/healthz" in PUBLIC_PATHS
    assert "/readyz" in PUBLIC_PATHS


def test_metrics_is_the_only_other_public_path() -> None:
    """Three exemptions, forever. A fourth is a review conversation, not a diff."""
    assert PUBLIC_PATHS == frozenset({"/healthz", "/readyz", "/metrics"})


def test_the_router_declares_no_auth_dependency() -> None:
    """Every other router carries `Depends(require_internal_service)` at router level."""
    assert health.router.dependencies == []
    assert health.router.prefix == ""


# --------------------------------------------------------------------------------------
# /healthz — liveness
# --------------------------------------------------------------------------------------


async def test_healthz_reports_the_service_and_an_uptime() -> None:
    body = await healthz()

    assert body["status"] == "ok"
    assert body["service"] == SERVICE_NAME == "payzeno-ledger"
    assert body["uptime_seconds"] >= 0


def test_liveness_never_opens_a_session() -> None:
    """A liveness probe with a `SessionFactory` is a liveness probe that will use it.

    Asserted on the signature because that is where the mistake would be introduced: a
    handler cannot round-trip the database it was never handed.
    """
    parameters = inspect.signature(healthz).parameters

    assert parameters == {}


async def test_healthz_is_cheap_enough_to_call_every_two_seconds() -> None:
    """Called by the ECS health check on all four tasks. It must not allocate work."""
    first = await healthz()
    second = await healthz()

    assert first["service"] == second["service"]
    assert second["uptime_seconds"] >= first["uptime_seconds"]


# --------------------------------------------------------------------------------------
# /readyz — readiness
# --------------------------------------------------------------------------------------


async def test_readyz_round_trips_select_one(sessions_factory) -> None:
    response = Response()

    body = await readyz(sessions_factory, response)

    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok"}
    assert sessions_factory.begin_count == 1
    assert "SELECT 1" in sessions_factory.last.executed[0]


async def test_a_healthy_readyz_leaves_the_status_code_alone(sessions_factory) -> None:
    response = Response()

    await readyz(sessions_factory, response)

    assert response.status_code == status.HTTP_200_OK


async def test_readyz_reports_latency(sessions_factory) -> None:
    response = Response()

    body = await readyz(sessions_factory, response)

    assert body["latency_ms"] >= 0
    assert body["latency_ms"] <= READINESS_BUDGET_MS


async def test_an_unreachable_database_is_a_503(failing_sessions) -> None:
    """Out of the load balancer, not out of the cluster.

    Forty seconds of a task taking no traffic during a failover is a blip. Restarting it
    is a cold start plus a connection-pool warm-up on every task at once.
    """
    response = Response()

    body = await readyz(failing_sessions, response)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "unavailable"


async def test_the_probe_reports_a_failure_rather_than_raising(failing_sessions) -> None:
    """`readyz` swallows everything.

    An exception out of a probe becomes a 500 through `error_handlers.py`, which is the
    same signal with a worse body and an entry in the error budget.
    """
    response = Response()

    body = await readyz(failing_sessions, response)

    assert body["service"] == SERVICE_NAME
    assert "latency_ms" in body


async def test_a_slow_database_is_degraded_not_ok(sessions_factory, monkeypatch) -> None:
    """Answering slowly is not the same as answering.

    `DATABASE_POOL_SIZE` is 20 and `reconcile_batch` holds three sessions per concurrent
    pass. When the pool is exhausted the probe does not fail — it waits. Reporting `slow`
    as `ok` is how a task keeps taking traffic it cannot serve.
    """
    monkeypatch.setattr(health, "READINESS_BUDGET_MS", -1)
    response = Response()

    body = await readyz(sessions_factory, response)

    assert body["checks"]["database"] == "slow"
    assert body["status"] == "degraded"
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


async def test_the_readiness_budget_is_one_second() -> None:
    """Anything slower and the database is not usable even though it answered."""
    assert READINESS_BUDGET_MS == 1_000
